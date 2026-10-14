"""Exercise lifecycle persistence as brain_app on the disposable database.

Single-connection tests roll back their writes. Concurrency fixtures commit only
on the private module database, which is dropped afterwards. No test mutates role
attributes or the shared databases.
"""

import asyncio
from collections.abc import AsyncIterator
from datetime import UTC, date, datetime, time, timedelta, timezone
from decimal import Decimal
from typing import Any
from unittest.mock import AsyncMock
from uuid import UUID, uuid4

import pytest
import pytest_asyncio
import sqlalchemy as sa
from sqlalchemy.exc import DBAPIError, IntegrityError
from sqlalchemy.ext.asyncio import AsyncConnection, AsyncEngine, create_async_engine

from brain_v42.db import tables as t
from brain_v42.knowledge.hysteresis import confirmed
from brain_v42.repositories import pg_knowledge_lifecycle as lifecycle
from brain_v42.repositories.pg_knowledge_lifecycle import (
    ApplyWindowClosed,
    InvalidRunTransition,
    PgKnowledgeLifecycleRepo,
    StaleAtApply,
    StaleRunState,
    apply_window_open,
    check_apply_window,
    whole_run_lock,
)

pytestmark = pytest.mark.integration
DAY = date(2026, 10, 14)
NOW = datetime(2026, 10, 14, 12, tzinfo=UTC)
DIGEST = "a" * 64
repo = PgKnowledgeLifecycleRepo()


@pytest_asyncio.fixture
async def conn(engine: AsyncEngine) -> AsyncIterator[AsyncConnection]:
    async with engine.connect() as connection:
        async with connection.begin() as transaction:
            await connection.execute(sa.text("SET LOCAL ROLE brain_app"))
            yield connection
            await transaction.rollback()


async def run(conn: AsyncConnection) -> str:
    run_id = "lifecycle-" + uuid4().hex
    await repo.create_run(conn, run_id, "apply", {})
    return run_id


async def prepare_apply(conn: AsyncConnection, run_id: str) -> None:
    chain = [
        "created",
        "manifest_frozen",
        "submitting",
        "submitted",
        "results_stored",
        "evaluated",
        "applying",
    ]
    for source, target in zip(chain, chain[1:], strict=False):
        kwargs = {"manifest_digest": DIGEST} if target == "manifest_frozen" else {}
        await repo.transition_run(conn, run_id, source, target, **kwargs)


async def entry(conn: AsyncConnection, **values: Any) -> UUID:
    entity_id = uuid4()
    await conn.execute(
        t.decisions.insert().values(
            id=entity_id,
            title="Lifecycle test",
            description="A claim",
            reasoning="Evidence",
            content_updated_at=NOW,
            **values,
        )
    )
    return entity_id


async def evaluation(
    conn: AsyncConnection,
    run_id: str,
    entity_id: UUID,
    *,
    justifications: list[dict[str, Any]] | None = None,
    **overrides: Any,
) -> dict[str, Any]:
    values = {
        "run_id": run_id,
        "entity_type": "decisions",
        "entity_id": entity_id,
        "entry_digest": DIGEST,
        "extraction_id": None,
        "expected_cache_revision": 0,
        "rules_version": "v1",
        "ranks_version": "v1",
        "registry_version": "v1",
        "origin_main_sha": "b" * 40,
        "observed_day": DAY,
        "complete": True,
        "state": "refuted",
        "reason": "contradicted_by_source",
        "fingerprint": "c" * 64,
    }
    values.update(overrides)
    return await repo.insert_evaluation(conn, values, justifications or [])


def observation(**overrides: Any) -> dict[str, Any]:
    values: dict[str, Any] = {
        "assertion_key": "claim",
        "polarity": "contradicts",
        "source_kind": "config",
        "source_rank": 5,
        "source_ref": "config:claim",
        "observed_value": "changed",
        "valid_from": None,
        "valid_from_precision": "unknown",
    }
    values.update(overrides)
    return values


async def apply(conn: AsyncConnection, ev: dict[str, Any], **overrides: Any) -> dict[str, Any]:
    if (await repo.read_run(conn, ev["run_id"]))["status"] == "created":
        await prepare_apply(conn, ev["run_id"])
    inputs = await repo.lock_apply_inputs(conn, ev["entity_type"], ev["entity_id"])
    return await repo.append_transition(
        conn,
        ev["id"],
        inputs,
        to_state=ev["state"],
        reason=ev["reason"],
        **overrides,
    )


async def test_runs_cas_metadata_and_invalidation(conn: AsyncConnection) -> None:
    run_id = await run(conn)
    assert (await repo.read_run(conn, run_id))["status"] == "created"
    await repo.transition_run(conn, run_id, "created", "manifest_frozen", manifest_digest=DIGEST)
    with pytest.raises(StaleRunState):
        await repo.transition_run(
            conn, run_id, "created", "manifest_frozen", manifest_digest=DIGEST
        )
    with pytest.raises(InvalidRunTransition):
        await repo.transition_run(conn, run_id, "manifest_frozen", "applied")
    await repo.record_run(
        conn,
        run_id,
        batch_id="batch-1",
        usage={"input_tokens": 42},
        real_cost=Decimal("0.12"),
        counts={"evaluated": 3},
    )
    await repo.record_run(conn, run_id, batch_id="batch-2")
    row = await repo.read_run(conn, run_id)
    assert row["batch_ids"] == ["batch-1", "batch-2"]
    assert row["usage"] == {"input_tokens": 42}
    assert row["real_cost"] == Decimal("0.12")
    await repo.transition_run(
        conn, run_id, "manifest_frozen", "failed", reason="ambiguous_submission"
    )
    assert (await repo.read_run(conn, run_id))["counts"]["failure_reason"] == "ambiguous_submission"
    with pytest.raises(InvalidRunTransition):
        await repo.transition_run(conn, run_id, "failed", "invalidated")
    assert await repo.invalidate_run(conn, run_id, "bad input") is True
    assert await repo.invalidate_run(conn, run_id, "different retry reason") is False
    row = await repo.read_run(conn, run_id)
    assert row["status"] == "invalidated"
    assert row["invalidation_reason"] == "bad input"
    assert row["invalidated_at"] is not None


async def test_failed_transition_requires_reason_and_frozen_digest(conn: AsyncConnection) -> None:
    run_id = await run(conn)
    with pytest.raises(ValueError):
        await repo.transition_run(conn, run_id, "created", "failed")
    with pytest.raises(ValueError):
        await repo.transition_run(conn, run_id, "created", "manifest_frozen")
    with pytest.raises(InvalidRunTransition):
        await repo.invalidate_run(conn, run_id, "not terminal")


async def test_requests_checkpoint_once_and_preserve_manifest(conn: AsyncConnection) -> None:
    run_id = await run(conn)
    custom_id = run_id + "-00000"
    manifest = {
        custom_id: {
            "entries": [
                {"entity_type": "decisions", "entity_id": str(uuid4()), "entry_digest": DIGEST}
            ]
        }
    }
    await repo.insert_requests(conn, run_id, manifest)
    await repo.insert_requests(conn, run_id, manifest)
    rows = await repo.pending_requests(conn, run_id)
    assert len(rows) == 1 and rows[0]["manifest_entry"] == manifest[custom_id]
    assert await repo.checkpoint_request(conn, run_id, custom_id, "succeeded") is True
    assert await repo.checkpoint_request(conn, run_id, custom_id, "invalid") is False
    assert await repo.pending_requests(conn, run_id) == []
    row = (
        (
            await conn.execute(
                sa.select(t.knowledge_lifecycle_requests).where(
                    t.knowledge_lifecycle_requests.c.run_id == run_id
                )
            )
        )
        .mappings()
        .one()
    )
    assert row["status"] == "succeeded" and row["result_stored_at"] is not None
    with pytest.raises(KeyError):
        await repo.checkpoint_request(conn, run_id, run_id + "-missing", "missing")
    with pytest.raises(ValueError):
        await repo.insert_requests(conn, run_id, {custom_id: {"entries": []}})


async def test_extraction_replay_keeps_original_and_revision(conn: AsyncConnection) -> None:
    run_id = await run(conn)
    entity_id = uuid4()
    kwargs = {
        "entity_type": "decisions",
        "entity_id": entity_id,
        "entry_digest": DIGEST,
        "model": "test-model",
        "prompt_version": "v1",
        "assertions": [],
    }
    first = await repo.insert_extraction(conn, run_id=run_id, **kwargs)
    assert await repo.insert_extraction(conn, run_id=await run(conn), **kwargs) == first
    assert (
        await repo.read_extraction(
            conn, "decisions", entity_id, DIGEST, model="test-model", prompt_version="v1"
        )
    )["id"] == first
    assert (
        await repo.read_extraction(
            conn, "decisions", entity_id, "new", model="test-model", prompt_version="v1"
        )
        is None
    )


async def test_evaluation_and_justifications_are_atomic_and_replayed(conn: AsyncConnection) -> None:
    run_id = await run(conn)
    entity_id = uuid4()
    first = await evaluation(conn, run_id, entity_id, justifications=[observation()])
    second = await evaluation(
        conn, run_id, entity_id, justifications=[observation(source_ref="new")]
    )
    assert second == first
    assert len(await repo.read_justifications(conn, first["id"])) == 1
    bad_id = uuid4()
    with pytest.raises(IntegrityError):
        await evaluation(conn, run_id, bad_id, justifications=[observation(source_rank=4)])
    assert await repo.entries_for_run(conn, run_id) == [("decisions", entity_id)]
    assert await repo.read_evaluation(conn, first["id"]) == first


async def test_history_excludes_incomplete_and_invalidated_but_keeps_disagreements(
    conn: AsyncConnection,
) -> None:
    entity_id = uuid4()
    first_run = await run(conn)
    await evaluation(conn, first_run, entity_id)
    await evaluation(conn, await run(conn), entity_id, complete=False, fingerprint="bad")
    later = date(2026, 10, 15)
    await evaluation(conn, await run(conn), entity_id, observed_day=later)
    history = await repo.evaluation_history(conn, "decisions", entity_id)
    assert len(history) == 2 and confirmed(history[:-1], history[-1])
    await repo.transition_run(conn, first_run, "created", "failed", reason="test")
    await repo.invalidate_run(conn, first_run, "wrong")
    history = await repo.evaluation_history(conn, "decisions", entity_id)
    assert len(history) == 1 and not confirmed([], history[0])
    await evaluation(conn, await run(conn), entity_id, fingerprint="disagrees")
    await evaluation(conn, await run(conn), entity_id, observed_day=date(2026, 10, 16))
    history = await repo.evaluation_history(conn, "decisions", entity_id)
    assert not confirmed(history[:-1], history[-1])


async def test_propagation_history_does_not_reset_check_agreement(conn: AsyncConnection) -> None:
    entity_id = uuid4()
    await evaluation(conn, await run(conn), entity_id)
    await evaluation(
        conn,
        await run(conn),
        entity_id,
        state="contested",
        reason="propagated_from_parent",
        justifications=[observation(source_kind="propagation", source_rank=2)],
    )
    await evaluation(conn, await run(conn), entity_id, observed_day=date(2026, 10, 15))
    history = await repo.evaluation_history(conn, "decisions", entity_id)
    assert history[1].layer == "propagation" and confirmed(history[:-1], history[-1])


async def test_apply_replay_and_stale_write_nothing(conn: AsyncConnection) -> None:
    entity_id = await entry(conn)
    run_id = await run(conn)
    ev = await evaluation(conn, run_id, entity_id)
    inputs = await repo.lock_apply_inputs(conn, "decisions", entity_id)
    assert inputs.entry["description"] == "A claim"
    assert inputs.revision == 0 and inputs.cache is None
    assert await conn.scalar(sa.text("SHOW lock_timeout")) == "5s"
    assert await conn.scalar(sa.text("SHOW statement_timeout")) == "30s"
    first = await apply(conn, ev)
    assert await apply(conn, ev) == first
    assert (await repo.read_cache(conn, "decisions", entity_id))["revision"] == 1
    assert (await repo.read_run(conn, run_id))["counts"]["applied_events"] == 1
    stale = await evaluation(conn, await run(conn), entity_id)
    with pytest.raises(StaleAtApply):
        await apply(conn, stale)
    assert await repo.events_for_entry(conn, "decisions", entity_id) == [first]
    assert (await repo.read_run(conn, stale["run_id"]))["counts"] == {}


async def test_apply_savepoint_keeps_no_event_when_cas_fails(conn: AsyncConnection) -> None:
    entity_id = await entry(conn)
    ev = await evaluation(conn, await run(conn), entity_id)
    inputs = await repo.lock_apply_inputs(conn, "decisions", entity_id)
    await apply(conn, ev)
    later = await evaluation(conn, await run(conn), entity_id)
    await prepare_apply(conn, later["run_id"])
    with pytest.raises(StaleAtApply):
        await repo.append_transition(conn, later["id"], inputs, to_state="contested", reason="test")
    assert len(await repo.events_for_entry(conn, "decisions", entity_id)) == 1


async def test_unmeasured_content_timestamp_uses_creation_lower_bound(
    conn: AsyncConnection,
) -> None:
    entity_id = await entry(conn)
    created_at = datetime(2026, 10, 1, tzinfo=UTC)
    await conn.execute(
        t.decisions.update()
        .where(t.decisions.c.id == entity_id)
        .values(content_updated_at=None, created_at=created_at)
    )
    ev = await evaluation(conn, await run(conn), entity_id, state="obsolete", reason="merged")
    event = await apply(conn, ev, valid_from=DAY, valid_from_precision="first_observed")
    assert await repo.events_for_entry(conn, "decisions", entity_id) == [event]
    cache = await repo.read_cache(conn, "decisions", entity_id)
    assert cache["entry_content_updated_at"] == created_at
    assert (await repo.lock_apply_inputs(conn, "decisions", entity_id)).entry[
        "content_updated_at"
    ] == created_at
    assert await repo.cache_mismatches(conn) == []
    await conn.execute(
        t.decisions.update().where(t.decisions.c.id == entity_id).values(content_updated_at=NOW)
    )
    inputs = await repo.lock_apply_inputs(conn, "decisions", entity_id)
    assert inputs.entry["content_updated_at"] > cache["entry_content_updated_at"]


async def test_same_transaction_content_change_defers_without_event(conn: AsyncConnection) -> None:
    entity_id = await entry(conn)
    ev = await evaluation(conn, await run(conn), entity_id)
    await prepare_apply(conn, ev["run_id"])
    inputs = await repo.lock_apply_inputs(conn, "decisions", entity_id)
    await conn.execute(
        t.decisions.update().where(t.decisions.c.id == entity_id).values(description="Changed")
    )
    with pytest.raises(StaleAtApply):
        await repo.append_transition(conn, ev["id"], inputs, to_state="refuted", reason="test")
    assert await repo.events_for_entry(conn, "decisions", entity_id) == []


async def test_embedding_and_access_metadata_are_not_apply_inputs(conn: AsyncConnection) -> None:
    entity_id = await entry(conn, embedding=[0.1] * 1536)
    ev = await evaluation(conn, await run(conn), entity_id)
    await prepare_apply(conn, ev["run_id"])
    inputs = await repo.lock_apply_inputs(conn, "decisions", entity_id)
    assert "embedding" not in inputs.entry and "access_count" not in inputs.entry
    await conn.execute(
        t.decisions.update().where(t.decisions.c.id == entity_id).values(access_count=1)
    )
    event = await repo.append_transition(conn, ev["id"], inputs, to_state="refuted", reason="test")
    assert event["to_state"] == "refuted"


async def test_requests_cannot_be_added_after_freeze(conn: AsyncConnection) -> None:
    run_id = await run(conn)
    await repo.transition_run(conn, run_id, "created", "manifest_frozen", manifest_digest=DIGEST)
    with pytest.raises(InvalidRunTransition):
        await repo.insert_requests(conn, run_id, {run_id + "-00001": {"entries": []}})
    assert await repo.pending_requests(conn, run_id) == []


async def test_custom_id_must_use_a_run_sequence(conn: AsyncConnection) -> None:
    run_id = await run(conn)
    for custom_id in ("another-run-00000", run_id + "-text", run_id + "-"):
        with pytest.raises(ValueError):
            await repo.insert_requests(conn, run_id, {custom_id: {"entries": []}})
    assert await repo.pending_requests(conn, run_id) == []


async def test_compensation_and_cache_replay(conn: AsyncConnection) -> None:
    entity_id = await entry(conn)
    ev1 = await evaluation(conn, await run(conn), entity_id, state="valid", reason="checked")
    await apply(conn, ev1)
    await repo.transition_run(conn, ev1["run_id"], "applying", "applied")
    run2 = await run(conn)
    ev2 = await evaluation(conn, run2, entity_id, expected_cache_revision=1)
    event2 = await apply(conn, ev2)
    await repo.transition_run(conn, run2, "applying", "failed", reason="test")
    await repo.invalidate_run(conn, run2, "wrong")
    inputs = await repo.lock_apply_inputs(conn, "decisions", entity_id)
    kwargs = {
        "evaluation_id": ev1["id"],
        "inputs": inputs,
        "invalidated_run_id": run2,
        "compensates_event_id": event2["id"],
        "expected_cache_revision": 2,
        "to_state": "valid",
        "reason": "recomputed",
    }
    compensation = await repo.append_compensation(conn, **kwargs)
    assert await repo.append_compensation(conn, **kwargs) == compensation
    counts = (await repo.read_run(conn, run2))["counts"]
    assert counts["applied_events"] == 1 and counts["compensation_events"] == 1
    assert (await repo.read_run(conn, ev1["run_id"]))["counts"] == {"applied_events": 1}
    events = await repo.events_for_entry(conn, "decisions", entity_id)
    assert [event["kind"] for event in events] == ["transition", "transition", "compensation"]
    rebuilt = await repo.replay_cache(conn)
    cache = await repo.read_cache(conn, "decisions", entity_id)
    assert all(cache[key] == value for key, value in rebuilt[("decisions", entity_id)].items())
    assert await repo.cache_mismatches(conn) == []
    await conn.execute(
        t.knowledge_lifecycle_state.update()
        .where(t.knowledge_lifecycle_state.c.entity_id == entity_id)
        .values(state="refuted")
    )
    assert await repo.cache_mismatches(conn) == [("decisions", entity_id)]


@pytest.mark.parametrize(
    ("mode", "complete"), [("dry_run", True), ("apply", False), ("dry_run", False)]
)
async def test_compensation_refuses_ineligible_evaluation(
    conn: AsyncConnection, mode: str, complete: bool
) -> None:
    entity_id = await entry(conn)
    invalidated_run_id = await run(conn)
    original = await evaluation(conn, invalidated_run_id, entity_id)
    event = await apply(conn, original)
    await repo.transition_run(conn, invalidated_run_id, "applying", "applied")
    await repo.invalidate_run(conn, invalidated_run_id, "wrong evidence")
    evaluation_run_id = "lifecycle-" + uuid4().hex
    await repo.create_run(conn, evaluation_run_id, mode, {})
    ev = await evaluation(conn, evaluation_run_id, entity_id, complete=complete)
    inputs = await repo.lock_apply_inputs(conn, "decisions", entity_id)
    cache_before = await repo.read_cache(conn, "decisions", entity_id)
    counts_before = (await repo.read_run(conn, invalidated_run_id))["counts"]
    with pytest.raises(
        lifecycle.InvalidApply, match="compensation_requires_complete_evaluation_and_apply_run"
    ):
        await repo.append_compensation(
            conn,
            evaluation_id=ev["id"],
            inputs=inputs,
            invalidated_run_id=invalidated_run_id,
            compensates_event_id=event["id"],
            expected_cache_revision=inputs.revision,
            to_state="valid",
            reason="recomputed",
        )
    assert await repo.events_for_entry(conn, "decisions", entity_id) == [event]
    assert await repo.read_cache(conn, "decisions", entity_id) == cache_before
    assert (await repo.read_run(conn, invalidated_run_id))["counts"] == counts_before
    assert (await repo.read_run(conn, evaluation_run_id))["counts"] == {}


async def test_inputs_scope_and_successor_join(conn: AsyncConnection) -> None:
    project = "lifecycle-" + uuid4().hex[:12]
    successor = await entry(conn, project_key=project)
    prior = await entry(conn, project_key=project, status="superseded", superseded_by=successor)
    rows = await repo.list_entries(conn, project)
    assert {row["id"] for row in rows} == {prior, successor}
    row = next(row for row in rows if row["id"] == prior)
    assert row["successor_exists"] is True and row["successor_created_at"] is not None
    assert row["entity_type"] == "decisions" and row["content_updated_at"] == NOW
    assert row["text_fields"]["reasoning"] == "Evidence"
    assert successor in {row["id"] for row in await repo.list_entries(conn)}


async def test_list_entries_coalesces_unmeasured_content_timestamp(conn: AsyncConnection) -> None:
    project = "lifecycle-" + uuid4().hex[:12]
    entity_id = await entry(conn, project_key=project)
    await conn.execute(
        t.decisions.update().where(t.decisions.c.id == entity_id).values(content_updated_at=None)
    )
    (row,) = await repo.list_entries(conn, project)
    assert row["content_updated_at"] == row["created_at"]


@pytest.mark.parametrize("successor_in_project", [False, True])
async def test_adr_successor_join_matches_locked_read_within_project(
    conn: AsyncConnection, successor_in_project: bool
) -> None:
    project = "lifecycle-" + uuid4().hex[:12]
    other_project = "lifecycle-" + uuid4().hex[:12]
    number = int(uuid4().hex[:7], 16)
    prior_id = uuid4()
    fixtures = [
        (prior_id, number, project, number + 1, NOW),
        (uuid4(), number + 1, other_project, None, NOW - timedelta(days=1)),
    ]
    if successor_in_project:
        fixtures.append((uuid4(), number + 1, project, None, NOW))
    for entity_id, adr_number, project_key, superseded_by, created_at in fixtures:
        await conn.execute(
            t.adrs.insert().values(
                id=entity_id,
                number=adr_number,
                project_key=project_key,
                title="Project-scoped ADR",
                context="Context",
                decision="Choice",
                consequences="Effect",
                superseded_by=superseded_by,
                created_at=created_at,
            )
        )
    rows = [row for row in await repo.list_entries(conn, project) if row["id"] == prior_id]
    assert len(rows) == 1
    locked = await repo.lock_apply_inputs(conn, "adrs", prior_id)
    for inputs in (rows[0], locked.entry):
        assert inputs["successor_exists"] is successor_in_project
        assert inputs["successor_created_at"] == (NOW if successor_in_project else None)


async def test_all_five_input_tables_and_project_scoped_adr_successors(
    conn: AsyncConnection,
) -> None:
    project = "lifecycle-" + uuid4().hex[:12]
    other_project = "lifecycle-" + uuid4().hex[:12]
    fixtures = {
        "decisions": {
            "title": "Decision",
            "description": "Claim",
            "reasoning": "Why",
            "alternatives": ["Other"],
        },
        "learnings": {"topic": "Learning", "insight": "Claim", "source": "Documentation"},
        "snippets": {
            "title": "Snippet",
            "intention": "Purpose",
            "code": "pass",
            "language": "python",
            "gotchas": "Caveat",
        },
        "runbooks": {
            "title": "Runbook",
            "description": "Procedure",
            "trigger": "Needed",
            "steps": [{"action": "Check"}],
        },
        "adrs": {
            "number": 1,
            "title": "ADR",
            "context": "Context",
            "decision": "Choice",
            "consequences": "Effect",
            "status": "superseded",
            "superseded_by": 2,
        },
    }
    identities = {}
    for name, fields in fixtures.items():
        table = getattr(t, name)
        entity_id = uuid4()
        identities[name] = entity_id
        await conn.execute(
            table.insert().values(
                id=entity_id, project_key=project, content_updated_at=NOW, **fields
            )
        )
    await conn.execute(
        t.adrs.insert().values(
            number=2,
            title="Unrelated successor",
            context="Context",
            decision="Choice",
            consequences="Effect",
            project_key=other_project,
        )
    )
    rows = await repo.list_entries(conn, project)
    assert {row["entity_type"]: row["id"] for row in rows} == identities
    by_type = {row["entity_type"]: row for row in rows}
    for row in rows:
        assert "merged_into" in row and "freshness_status_updated_at" in row
        assert row["content_updated_at"] == NOW
        inputs = await repo.lock_apply_inputs(conn, row["entity_type"], row["id"])
        assert inputs.entry["text_fields"] == row["text_fields"]
    assert by_type["snippets"]["text_fields"]["code"] == "pass"
    assert by_type["learnings"]["text_fields"]["insight"] == "Claim"
    assert by_type["runbooks"]["text_fields"]["steps"] == [{"action": "Check"}]
    assert by_type["adrs"]["successor_exists"] is False
    await conn.execute(
        t.adrs.insert().values(
            number=2,
            title="Actual successor",
            context="Context",
            decision="Choice",
            consequences="Effect",
            project_key=project,
            created_at=NOW,
        )
    )
    inputs = await repo.lock_apply_inputs(conn, "adrs", identities["adrs"])
    assert inputs.entry["successor_exists"] is True and inputs.entry["successor_created_at"] == NOW


@pytest.mark.parametrize(
    "status", ["succeeded", "errored", "expired", "canceled", "missing", "invalid"]
)
async def test_every_request_checkpoint_status(conn: AsyncConnection, status: str) -> None:
    run_id = await run(conn)
    custom_id = run_id + "-00000"
    await repo.insert_requests(conn, run_id, {custom_id: {"entries": []}})
    assert await repo.checkpoint_request(conn, run_id, custom_id, status)
    assert not await repo.checkpoint_request(conn, run_id, custom_id, "invalid")
    assert await repo.pending_requests(conn, run_id) == []


async def test_obsolete_event_requires_an_end_date_and_rolls_back(conn: AsyncConnection) -> None:
    entity_id = await entry(conn)
    ev = await evaluation(conn, await run(conn), entity_id, state="obsolete")
    with pytest.raises(IntegrityError):
        await apply(conn, ev)
    assert await repo.events_for_entry(conn, "decisions", entity_id) == []
    event = await apply(conn, ev, valid_from=DAY, valid_from_precision="first_observed")
    assert event["valid_from"] == DAY


async def test_structural_history_retains_its_immediate_layer(conn: AsyncConnection) -> None:
    entity_id = uuid4()
    await evaluation(conn, await run(conn), entity_id, state="obsolete", reason="merged")
    history = await repo.evaluation_history(conn, "decisions", entity_id)
    assert history[0].layer == "structural" and confirmed([], history[0])


async def test_event_cache_and_counters_remain_in_caller_transaction(conn: AsyncConnection) -> None:
    entity_id = await entry(conn)
    run_id = await run(conn)
    ev = await evaluation(conn, run_id, entity_id)
    transaction = await conn.begin_nested()
    await apply(conn, ev)
    await transaction.rollback()
    assert await repo.events_for_entry(conn, "decisions", entity_id) == []
    assert await repo.read_cache(conn, "decisions", entity_id) is None
    assert (await repo.read_run(conn, run_id))["counts"] == {}


async def test_all_forward_run_checkpoints(conn: AsyncConnection) -> None:
    run_id = await run(conn)
    chain = [
        "created",
        "manifest_frozen",
        "submitting",
        "submitted",
        "results_stored",
        "evaluated",
        "applying",
        "applied",
    ]
    for source, target in zip(chain, chain[1:], strict=False):
        kwargs = {"manifest_digest": DIGEST} if target == "manifest_frozen" else {}
        row = await repo.transition_run(conn, run_id, source, target, **kwargs)
        assert row["status"] == target
    assert row["ended_at"] is not None
    assert await repo.invalidate_run(conn, run_id, "wrong")


async def test_direct_dependents_follow_source_to_target_only(conn: AsyncConnection) -> None:
    ids = [uuid4() for _ in range(4)]
    for entity_id in ids:
        await conn.execute(
            t.brain_entities.insert().values(
                entity_type="decision",
                entity_key=str(entity_id),
                source_uuid=entity_id,
                scope_kind="global",
                id=uuid4(),
            )
        )
    graph_ids = dict(
        (
            await conn.execute(
                sa.select(t.brain_entities.c.source_uuid, t.brain_entities.c.id).where(
                    t.brain_entities.c.source_uuid.in_(ids)
                )
            )
        ).all()
    )
    for source, target, label in [
        (ids[1], ids[0], "MOTIVATED_BY"),
        (ids[1], ids[0], "IMPLEMENTS"),
        (ids[2], ids[1], "DEPENDS_ON"),
        (ids[0], ids[3], "IMPLEMENTS"),
    ]:
        await conn.execute(
            t.entity_relations.insert().values(
                source_entity_id=graph_ids[source],
                target_entity_id=graph_ids[target],
                relation_type=label,
                origin="test",
            )
        )
    assert await repo.direct_dependents(conn, "decisions", ids[0]) == [("decisions", ids[1])]


async def test_lock_contends_and_releases_on_exception(engine: AsyncEngine) -> None:
    with pytest.raises(RuntimeError, match="test exit"):
        async with whole_run_lock(engine) as first:
            assert first is True
            async with whole_run_lock(engine) as second:
                assert second is False
            raise RuntimeError("test exit")
    async with whole_run_lock(engine) as after:
        assert after is True


@pytest.mark.parametrize("error_type", [RuntimeError, asyncio.CancelledError])
async def test_whole_run_lock_invalidates_interrupted_acquire(
    private_head_engine: AsyncEngine,
    monkeypatch: pytest.MonkeyPatch,
    error_type: type[BaseException],
) -> None:
    # A one-connection pool exposes a lock leaked back onto the physical session.
    pooled_engine = create_async_engine(private_head_engine.url, pool_size=1, max_overflow=0)
    original_scalar = AsyncConnection.scalar
    interrupted_pid = None

    async def interrupt_after_grant(
        connection: AsyncConnection, statement: Any, *args: Any, **kwargs: Any
    ) -> Any:
        nonlocal interrupted_pid
        result = await original_scalar(connection, statement, *args, **kwargs)
        if "pg_try_advisory_lock" in str(statement) and interrupted_pid is None:
            assert result is True
            interrupted_pid = await original_scalar(connection, sa.select(sa.func.pg_backend_pid()))
            # Model a grant whose reply never reaches whole_run_lock's caller.
            raise error_type("interrupted acquire")
        return result

    monkeypatch.setattr(AsyncConnection, "scalar", interrupt_after_grant)
    try:
        with pytest.raises(error_type, match="interrupted acquire"):
            async with whole_run_lock(pooled_engine):
                pytest.fail("An interrupted acquire must not enter the lock body")
        assert interrupted_pid is not None
        async with private_head_engine.connect() as observer:
            available = await observer.scalar(
                sa.select(sa.func.pg_try_advisory_lock(lifecycle.LIFECYCLE_RUN_LOCK))
            )
            try:
                assert available is True
            finally:
                if available:
                    await observer.execute(
                        sa.select(sa.func.pg_advisory_unlock(lifecycle.LIFECYCLE_RUN_LOCK))
                    )
        async with pooled_engine.connect() as replacement:
            assert await replacement.scalar(sa.select(sa.func.pg_backend_pid())) != interrupted_pid
        async with whole_run_lock(pooled_engine) as acquired:
            assert acquired is True
    finally:
        await pooled_engine.dispose()


async def test_window_uses_server_clock(conn: AsyncConnection) -> None:
    before = await conn.scalar(sa.text("SELECT (clock_timestamp() AT TIME ZONE 'UTC')::time"))
    try:
        await check_apply_window(conn)
    except ApplyWindowClosed:
        assert not apply_window_open(before)
    else:
        assert apply_window_open(before)


@pytest.mark.parametrize(
    "instant", [time(1, tzinfo=UTC), time(12, tzinfo=timezone(timedelta(hours=2)))]
)
def test_apply_window_rejects_aware_time(instant: time) -> None:
    with pytest.raises(ValueError, match="naive UTC"):
        apply_window_open(instant)


@pytest.mark.parametrize(
    ("instant", "allowed"),
    [(time(0, 49, 59), True), (time(0, 50), False), (time(3, 59, 59), False), (time(4), True)],
)
def test_apply_window_exact_boundaries(instant: time, allowed: bool) -> None:
    assert apply_window_open(instant) is allowed


async def test_check_apply_window_refuses_closed_server_clock() -> None:
    connection = AsyncMock(spec=AsyncConnection)
    connection.scalar.return_value = time(1)
    with pytest.raises(ApplyWindowClosed, match="apply_window_closed"):
        await check_apply_window(connection)
    statement = connection.scalar.call_args.args[0]
    assert "clock_timestamp()" in str(statement) and "AT TIME ZONE 'UTC'" in str(statement)


async def test_check_apply_window_accepts_open_server_clock() -> None:
    connection = AsyncMock(spec=AsyncConnection)
    connection.scalar.return_value = time(12)
    await check_apply_window(connection)
    connection.scalar.assert_awaited_once()


async def test_whole_run_lock_connection_is_idle(engine: AsyncEngine) -> None:
    from brain_v42.repositories.pg_knowledge_lifecycle import LIFECYCLE_RUN_LOCK

    async with whole_run_lock(engine) as acquired:
        assert acquired
        async with engine.connect() as observer:
            states = (
                await observer.execute(
                    sa.text(
                        "SELECT a.state, a.xact_start FROM pg_stat_activity a "
                        "JOIN pg_locks l ON l.pid = a.pid "
                        "WHERE l.locktype = 'advisory' AND l.granted "
                        "AND l.classid::bigint = :high AND l.objid::bigint = :low "
                        "AND l.objsubid = 1 AND a.datname = current_database()"
                    ),
                    {"high": LIFECYCLE_RUN_LOCK >> 32, "low": LIFECYCLE_RUN_LOCK & 0xFFFFFFFF},
                )
            ).all()
            assert len(states) == 1
            assert states[0].state == "idle" and states[0].xact_start is None


@pytest.mark.parametrize(
    ("mode", "status", "invalidated", "complete"),
    [
        ("dry_run", "applying", False, True),
        *(
            ("apply", status, False, True)
            for status in (
                "created",
                "manifest_frozen",
                "submitting",
                "submitted",
                "results_stored",
                "evaluated",
                "applied",
                "failed",
                "invalidated",
            )
        ),
        ("apply", "applying", True, True),
        ("apply", "applying", False, False),
    ],
)
async def test_transition_refuses_ineligible_run_or_evaluation(
    conn: AsyncConnection, mode: str, status: str, invalidated: bool, complete: bool
) -> None:
    entity_id = await entry(conn)
    run_id = await run(conn)
    await conn.execute(
        t.knowledge_lifecycle_runs.update()
        .where(t.knowledge_lifecycle_runs.c.run_id == run_id)
        .values(
            mode=mode,
            status=status,
            invalidated_at=NOW if invalidated else None,
            invalidation_reason="test" if invalidated else None,
        )
    )
    ev = await evaluation(conn, run_id, entity_id, complete=complete)
    inputs = await repo.lock_apply_inputs(conn, "decisions", entity_id)
    with pytest.raises(RuntimeError) as error:
        await repo.append_transition(conn, ev["id"], inputs, to_state="refuted", reason="test")
    assert type(error.value) is lifecycle.InvalidApply
    assert await repo.events_for_entry(conn, "decisions", entity_id) == []
    assert await repo.read_cache(conn, "decisions", entity_id) is None
    assert (await repo.read_run(conn, run_id))["counts"] == {}


async def test_transition_replay_survives_completed_run(conn: AsyncConnection) -> None:
    entity_id = await entry(conn)
    run_id = await run(conn)
    ev = await evaluation(conn, run_id, entity_id)
    event = await apply(conn, ev)
    await repo.transition_run(conn, run_id, "applying", "applied")
    assert await apply(conn, ev) == event
    assert (await repo.read_run(conn, run_id))["counts"] == {"applied_events": 1}


async def test_list_entries_successor_queries_do_not_grow_with_corpus(
    conn: AsyncConnection,
) -> None:
    project = "lifecycle-" + uuid4().hex[:12]
    successor = await entry(conn, project_key=project)
    priors = [
        await entry(conn, project_key=project, status="superseded", superseded_by=successor)
        for _ in range(12)
    ]
    queries = []

    def count_queries(*args: Any) -> None:
        queries.append(args[2])

    sa.event.listen(conn.sync_connection, "before_cursor_execute", count_queries)
    try:
        rows = await repo.list_entries(conn, project)
    finally:
        sa.event.remove(conn.sync_connection, "before_cursor_execute", count_queries)
    assert len(queries) <= 5
    assert {row["id"] for row in rows} == {*priors, successor}
    assert all(row["successor_exists"] for row in rows if row["id"] in priors)


async def test_evaluation_links_real_extraction_with_composite_identity(
    conn: AsyncConnection,
) -> None:
    run_id = await run(conn)
    entity_id = await entry(conn)
    extraction_id = await repo.insert_extraction(
        conn,
        run_id=run_id,
        entity_type="decisions",
        entity_id=entity_id,
        entry_digest=DIGEST,
        model="test-model",
        prompt_version="v1",
        assertions=[],
    )
    ev = await evaluation(conn, run_id, entity_id, extraction_id=extraction_id)
    assert ev["extraction_id"] == extraction_id
    assert (await repo.read_evaluation(conn, ev["id"]))["extraction_id"] == extraction_id
    for overrides in (
        {"entity_id": uuid4()},
        {"entity_type": "learnings"},
        {"entry_digest": "bad"},
    ):
        values = {"entity_id": entity_id, "extraction_id": extraction_id, **overrides}
        with pytest.raises(IntegrityError):
            await evaluation(conn, await run(conn), **values)
    assert await repo.entries_for_run(conn, run_id) == [("decisions", entity_id)]


async def committed_apply_entry(engine: AsyncEngine) -> tuple[UUID, UUID]:
    """Make rows visible to both connections only on the private disposable DB."""
    async with engine.begin() as setup:
        await setup.execute(sa.text("SET LOCAL ROLE brain_app"))
        successor = await entry(setup)
        entity_id = await entry(setup, status="superseded", superseded_by=successor)
        ev = await evaluation(setup, await run(setup), entity_id)
        await apply(setup, ev)
    return entity_id, successor


@pytest.mark.parametrize("blocked_row", ["entry", "cache", "successor"])
async def test_apply_input_locks_contend_between_connections(
    private_head_engine: AsyncEngine, blocked_row: str
) -> None:
    entity_id, successor = await committed_apply_entry(private_head_engine)
    table = t.knowledge_lifecycle_state if blocked_row == "cache" else t.decisions
    predicate = (
        table.c.entity_id == entity_id
        if blocked_row == "cache"
        else table.c.id == (successor if blocked_row == "successor" else entity_id)
    )
    async with private_head_engine.connect() as holder, private_head_engine.connect() as waiter:
        async with holder.begin(), waiter.begin():
            await holder.execute(sa.text("SET LOCAL ROLE brain_app"))
            await waiter.execute(sa.text("SET LOCAL ROLE brain_app"))
            await holder.execute(sa.select(table).where(predicate).with_for_update())
            async with waiter.begin_nested():
                with pytest.raises(RuntimeError) as error:
                    # Roll back the failed statement before continuing this transaction.
                    async with waiter.begin_nested():
                        await repo.lock_apply_inputs(
                            waiter, "decisions", entity_id, lock_timeout_ms=100
                        )
                assert isinstance(error.value, lifecycle.ApplyDeferred)
                assert error.value.sqlstate == "55P03"
            assert await waiter.scalar(sa.select(sa.literal(1))) == 1
            await holder.rollback()
            inputs = await repo.lock_apply_inputs(waiter, "decisions", entity_id)
            assert inputs.revision == 1


@pytest.mark.parametrize("updated_row", ["entry", "cache", "successor"])
async def test_apply_input_locks_block_concurrent_updates(
    private_head_engine: AsyncEngine, updated_row: str
) -> None:
    entity_id, successor = await committed_apply_entry(private_head_engine)
    table = t.knowledge_lifecycle_state if updated_row == "cache" else t.decisions
    predicate = (
        table.c.entity_id == entity_id
        if updated_row == "cache"
        else table.c.id == (successor if updated_row == "successor" else entity_id)
    )
    values = {"reason": "changed"} if updated_row == "cache" else {"description": "Changed"}
    async with private_head_engine.connect() as holder, private_head_engine.connect() as waiter:
        async with holder.begin(), waiter.begin():
            await holder.execute(sa.text("SET LOCAL ROLE brain_app"))
            await waiter.execute(sa.text("SET LOCAL ROLE brain_app"))
            await repo.lock_apply_inputs(holder, "decisions", entity_id)
            await waiter.execute(sa.text("SET LOCAL lock_timeout = '100ms'"))
            with pytest.raises(DBAPIError) as error:
                async with waiter.begin_nested():
                    await waiter.execute(table.update().where(predicate).values(**values))
            assert error.value.orig.sqlstate == "55P03"
            await holder.rollback()
            await waiter.execute(table.update().where(predicate).values(**values))
            await waiter.rollback()


@pytest.mark.parametrize(
    ("sqlstate", "label"), [("55P03", "lock_timeout"), ("57014", "query_canceled")]
)
def test_apply_deferred_labels_sqlstate(sqlstate: str, label: str) -> None:
    error = lifecycle.ApplyDeferred(sqlstate)
    assert error.sqlstate == sqlstate
    assert str(error) == label


async def test_statement_timeout_is_typed_apply_deferral(private_head_engine: AsyncEngine) -> None:
    entity_id, _ = await committed_apply_entry(private_head_engine)
    async with private_head_engine.connect() as holder, private_head_engine.connect() as waiter:
        async with holder.begin(), waiter.begin():
            await holder.execute(sa.text("SET LOCAL ROLE brain_app"))
            await waiter.execute(sa.text("SET LOCAL ROLE brain_app"))
            await holder.execute(
                sa.select(t.decisions).where(t.decisions.c.id == entity_id).with_for_update()
            )
            with pytest.raises(RuntimeError) as error:
                async with waiter.begin_nested():
                    await repo.lock_apply_inputs(
                        waiter,
                        "decisions",
                        entity_id,
                        lock_timeout_ms=1000,
                        statement_timeout_ms=100,
                    )
            assert isinstance(error.value, lifecycle.ApplyDeferred)
            assert error.value.sqlstate == "57014"
            assert str(error.value) == "query_canceled"
            assert await waiter.scalar(sa.select(sa.literal(1))) == 1
            await holder.rollback()
            await waiter.rollback()


async def test_append_transition_preserves_caller_timeouts_on_entry_relock(
    private_head_engine: AsyncEngine,
) -> None:
    entity_id, _ = await committed_apply_entry(private_head_engine)
    async with private_head_engine.begin() as setup:
        await setup.execute(sa.text("SET LOCAL ROLE brain_app"))
        run_id = await run(setup)
        ev = await evaluation(setup, run_id, entity_id, expected_cache_revision=1)
        await prepare_apply(setup, run_id)
    async with private_head_engine.connect() as holder, private_head_engine.connect() as waiter:
        async with holder.begin(), waiter.begin():
            await holder.execute(sa.text("SET LOCAL ROLE brain_app"))
            await waiter.execute(sa.text("SET LOCAL ROLE brain_app"))
            await waiter.execute(sa.text("SET LOCAL lock_timeout = '100ms'"))
            await waiter.execute(sa.text("SET LOCAL statement_timeout = '1s'"))
            snapshot = await waiter.begin_nested()
            inputs = await repo.lock_apply_inputs(
                waiter,
                "decisions",
                entity_id,
                lock_timeout_ms=100,
                statement_timeout_ms=1000,
            )
            # Release the snapshot's row locks while retaining the outer timeouts.
            await snapshot.rollback()
            await holder.execute(
                sa.select(t.decisions).where(t.decisions.c.id == entity_id).with_for_update()
            )
            with pytest.raises(lifecycle.ApplyDeferred, match="lock_timeout") as error:
                await asyncio.wait_for(
                    repo.append_transition(
                        waiter, ev["id"], inputs, to_state="contested", reason="test"
                    ),
                    timeout=2,
                )
            assert error.value.sqlstate == "55P03"
            assert isinstance(error.value.__cause__, DBAPIError)
            assert await waiter.scalar(sa.text("SHOW lock_timeout")) == "100ms"
            assert await waiter.scalar(sa.text("SHOW statement_timeout")) == "1s"
            assert len(await repo.events_for_entry(waiter, "decisions", entity_id)) == 1
            assert (await repo.read_run(waiter, run_id))["counts"] == {}
            await holder.rollback()
            event = await repo.append_transition(
                waiter, ev["id"], inputs, to_state="contested", reason="test"
            )
            assert event["to_state"] == "contested"
            assert await waiter.scalar(sa.text("SHOW lock_timeout")) == "100ms"
            assert await waiter.scalar(sa.text("SHOW statement_timeout")) == "1s"
            await waiter.rollback()


async def test_run_lock_timeout_is_typed_apply_deferral(private_head_engine: AsyncEngine) -> None:
    entity_id, _ = await committed_apply_entry(private_head_engine)
    async with private_head_engine.begin() as setup:
        await setup.execute(sa.text("SET LOCAL ROLE brain_app"))
        run_id = await run(setup)
        ev = await evaluation(setup, run_id, entity_id, expected_cache_revision=1)
        await prepare_apply(setup, run_id)
    async with private_head_engine.connect() as holder, private_head_engine.connect() as waiter:
        async with holder.begin(), waiter.begin():
            await holder.execute(sa.text("SET LOCAL ROLE brain_app"))
            await waiter.execute(sa.text("SET LOCAL ROLE brain_app"))
            await holder.execute(
                sa.select(t.knowledge_lifecycle_runs)
                .where(t.knowledge_lifecycle_runs.c.run_id == run_id)
                .with_for_update()
            )
            inputs = await repo.lock_apply_inputs(
                waiter, "decisions", entity_id, lock_timeout_ms=100
            )
            with pytest.raises(RuntimeError) as error:
                await repo.append_transition(
                    waiter, ev["id"], inputs, to_state="contested", reason="test"
                )
            assert isinstance(error.value, lifecycle.ApplyDeferred)
            assert error.value.sqlstate == "55P03"
            assert isinstance(error.value.__cause__, DBAPIError)
            assert len(await repo.events_for_entry(waiter, "decisions", entity_id)) == 1
            assert (await repo.read_run(waiter, run_id))["counts"] == {}
            await holder.rollback()
            event = await repo.append_transition(
                waiter, ev["id"], inputs, to_state="contested", reason="test"
            )
            assert event["to_state"] == "contested"
            await waiter.rollback()
