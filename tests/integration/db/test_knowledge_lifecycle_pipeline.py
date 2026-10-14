"""Commit lifecycle checkpoints only on the disposable module database."""

from datetime import UTC, datetime, timedelta
from types import SimpleNamespace
from typing import Any
from uuid import uuid4

import pytest
import sqlalchemy as sa
from sqlalchemy.ext.asyncio import AsyncConnection, AsyncEngine

from brain_v42.db import tables as t
from brain_v42.knowledge import extraction, facts_registry
from brain_v42.knowledge.checks import Sources
from brain_v42.knowledge.decide import Decision
from brain_v42.knowledge.ranks import State
from brain_v42.repositories.pg_knowledge_lifecycle import (
    ApplyWindowClosed,
    PgKnowledgeLifecycleRepo,
    apply_window_open,
)
from brain_v42.services.knowledge_lifecycle_pipeline import (
    BackupProof,
    EvaluationSources,
    LifecyclePipeline,
)

pytestmark = pytest.mark.integration


class Clock:
    def __init__(self) -> None:
        self.at = datetime.now(UTC).replace(hour=12, minute=0)

    def now(self) -> datetime:
        return self.at


class Gates:
    async def check(self, mode: str) -> None:
        return None


def fake_commit_gate(clock: Clock) -> Any:
    async def gate(conn: AsyncConnection) -> None:
        if not apply_window_open(clock.now().time().replace(tzinfo=None)):
            raise ApplyWindowClosed("apply_window_closed")

    return gate


class SourceProvider:
    async def snapshot(self, day: Any) -> EvaluationSources:
        return EvaluationSources(
            Sources(
                frozenset(),
                frozenset(),
                {},
                {},
                {"ci.system": "github-actions"},
                facts_registry.REGISTRY,
                "a" * 40,
            )
        )


class Client:
    async def count(self, manifest: extraction.Manifest) -> extraction.TokenCounts:
        return extraction.count(
            manifest,
            SimpleNamespace(
                messages=SimpleNamespace(
                    count_tokens=lambda **kwargs: SimpleNamespace(input_tokens=100),
                )
            ),
        )

    async def submit(self, manifest: extraction.Manifest) -> str:
        return "fake-batch"

    async def collect(
        self, batch_id: str, manifest: extraction.Manifest
    ) -> extraction.CollectedResults:
        assertion: extraction.Assertion = {
            "assertion_key": "claim",
            "quote": "gitlab-ci",
            "check": "fact",
            "repo": "brain-v42",
            "ref": "ci.system",
            "asserts": "exists",
            "claimed_status": "unknown",
            "fact_key": "ci.system",
            "claimed_value": "gitlab-ci",
            "centrality": "core",
            "temporal": "durable",
        }
        return extraction.CollectedResults(
            {
                r.custom_id: extraction.RequestResult(
                    extraction.ResultStatus.SUCCEEDED,
                    tuple(
                        extraction.Extraction(e.entity_type, e.entity_id, e.digest, (assertion,))
                        for e in r.entries
                    ),
                    extraction.Usage(100, 20),
                )
                for r in manifest.requests
            }
        )


async def seed(engine: AsyncEngine) -> tuple[str, Any]:
    project, entity_id = "pipeline-" + uuid4().hex, uuid4()
    async with engine.begin() as conn:
        await conn.execute(
            t.decisions.insert().values(
                id=entity_id,
                title="gitlab-ci",
                description="gitlab-ci",
                reasoning="claim",
                project_key=project,
                content_updated_at=datetime.now(UTC),
            )
        )
    return project, entity_id


async def seed_dependents(engine: AsyncEngine, count: int = 1) -> tuple[str, Any, list[Any]]:
    project, parent = await seed(engine)
    children = [(await seed(engine))[1] for _ in range(count)]
    _, successor = await seed(engine)
    async with engine.begin() as conn:
        await conn.execute(
            t.decisions.update()
            .where(t.decisions.c.id == parent)
            .values(status="superseded", superseded_by=successor)
        )
        graph_ids = dict(
            (
                await conn.execute(
                    sa.select(t.brain_entities.c.source_uuid, t.brain_entities.c.id).where(
                        t.brain_entities.c.entity_type == "decision",
                        t.brain_entities.c.source_uuid.in_([parent, *children]),
                    )
                )
            ).all()
        )
        for child in children:
            await conn.execute(
                t.entity_relations.insert().values(
                    source_entity_id=graph_ids[child],
                    target_entity_id=graph_ids[parent],
                    relation_type="DEPENDS_ON",
                    origin="test",
                )
            )
    return project, parent, children


async def test_lifecycle_resume_failed_child_fails_parent_on_postgres(
    private_head_engine: AsyncEngine, monkeypatch: pytest.MonkeyPatch
) -> None:
    import brain_v42.services.knowledge_lifecycle_pipeline as module

    project, parent, children = await seed_dependents(private_head_engine)
    clock = Clock()
    pipeline = LifecyclePipeline(
        private_head_engine,
        Client(),
        SourceProvider(),
        clock,
        Gates(),
        commit_gate=fake_commit_gate(clock),
    )
    original_decide, original_fail = module.decide, pipeline._fail

    def over_cap(observations: Any, day: Any) -> Decision:
        if observations and observations[0].source_kind == "propagation":
            return Decision(State.REFUTED, "bad_propagation", (), layer="propagation")
        return original_decide(observations, day)

    async def crash(run: Any, reason: str) -> Any:
        raise RuntimeError("interrupted after child failure")

    monkeypatch.setattr(module, "decide", over_cap)
    monkeypatch.setattr(pipeline, "_fail", crash)
    with pytest.raises(RuntimeError, match="interrupted after child failure"):
        await pipeline.start("apply", {"project_key": project}, 1, BackupProof("b", clock.now()))
    repo = PgKnowledgeLifecycleRepo()
    async with private_head_engine.connect() as conn:
        run_id = await conn.scalar(
            sa.select(t.knowledge_lifecycle_runs.c.run_id).where(
                t.knowledge_lifecycle_runs.c.scope == {"project_key": project}
            )
        )
        run = await repo.read_run(conn, run_id)
        child_run_id = run["counts"]["propagation_run_id"]
        assert run["status"] == "applying"
        assert (await repo.read_run(conn, child_run_id))["status"] == "failed"
    monkeypatch.setattr(module, "decide", original_decide)
    monkeypatch.setattr(pipeline, "_fail", original_fail)
    result = await pipeline.resume(run_id, BackupProof("resume", clock.now()))
    assert result.status == "failed" and result.failure_reason == "propagation_cap_exceeded"
    assert result.offending_ids == [str(children[0])]
    async with private_head_engine.connect() as conn:
        run = await repo.read_run(conn, run_id)
        assert run["counts"]["propagation_run_id"] == child_run_id
        assert run["counts"]["offending_ids"] == result.offending_ids
        assert len(await repo.events_for_entry(conn, "decisions", parent)) == 1
        assert await repo.events_for_entry(conn, "decisions", children[0]) == []
        assert await repo.cache_mismatches(conn) == []


async def test_lifecycle_invalidation_offender_is_persisted_on_postgres(
    private_head_engine: AsyncEngine, monkeypatch: pytest.MonkeyPatch
) -> None:
    project, parent, children = await seed_dependents(private_head_engine)
    clock = Clock()
    pipeline = LifecyclePipeline(
        private_head_engine,
        Client(),
        SourceProvider(),
        clock,
        Gates(),
        commit_gate=fake_commit_gate(clock),
    )
    applied = await pipeline.start(
        "apply", {"project_key": project}, 1, BackupProof("apply-backup", clock.now())
    )
    original = pipeline._propagation_decision

    async def over_cap(
        conn: Any, identity: Any, parents: Any, *, invalidating: bool = False
    ) -> Any:
        if invalidating and identity[1] == children[0]:
            return Decision(State.REFUTED, "bad_lift", (), layer="propagation")
        return await original(conn, identity, parents, invalidating=invalidating)

    monkeypatch.setattr(pipeline, "_propagation_decision", over_cap)
    result = await pipeline.invalidate(
        applied.run_id, "bad structure", BackupProof("invalidate-backup", clock.now())
    )
    assert result.status == "invalidated" and result.failure_reason == "propagation_cap_exceeded"
    assert result.offending_ids == [str(children[0])]
    repo = PgKnowledgeLifecycleRepo()
    async with private_head_engine.connect() as conn:
        run = await repo.read_run(conn, applied.run_id)
        assert run["counts"]["failure_reason"] == result.failure_reason
        assert run["counts"]["offending_ids"] == result.offending_ids
        assert (await repo.read_run(conn, run["counts"]["propagation_run_id"]))[
            "status"
        ] == "invalidated"
        assert (await repo.read_cache(conn, "decisions", parent))["state"] == "valid"
        assert (await repo.read_cache(conn, "decisions", children[0]))["state"] == "contested"
        assert not any(
            e["kind"] == "compensation"
            for e in await repo.events_for_entry(conn, "decisions", children[0])
        )
        assert await repo.cache_mismatches(conn) == []
        assert run["counts"]["backup_apply"]["run_id"] == "apply-backup"
        assert run["counts"]["backup_invalidate"]["run_id"] == "invalidate-backup"
    assert (await pipeline.report(applied.run_id)).backup_run_id == "invalidate-backup"


async def test_lifecycle_resume_mid_sweep_preserves_committed_events_on_postgres(
    private_head_engine: AsyncEngine, monkeypatch: pytest.MonkeyPatch
) -> None:
    project, parent, children = await seed_dependents(private_head_engine, count=2)
    clock = Clock()
    pipeline = LifecyclePipeline(
        private_head_engine,
        Client(),
        SourceProvider(),
        clock,
        Gates(),
        commit_gate=fake_commit_gate(clock),
    )
    original = pipeline._propagation_entry
    calls = 0

    async def crash(*args: Any, **kwargs: Any) -> None:
        nonlocal calls
        calls += 1
        if calls == 2:
            raise RuntimeError("interrupted sweep")
        await original(*args, **kwargs)

    monkeypatch.setattr(pipeline, "_propagation_entry", crash)
    with pytest.raises(RuntimeError, match="interrupted sweep"):
        await pipeline.start("apply", {"project_key": project}, 1, BackupProof("b", clock.now()))
    repo = PgKnowledgeLifecycleRepo()
    async with private_head_engine.connect() as conn:
        run_id = await conn.scalar(
            sa.select(t.knowledge_lifecycle_runs.c.run_id).where(
                t.knowledge_lifecycle_runs.c.scope == {"project_key": project}
            )
        )
        run = await repo.read_run(conn, run_id)
        child_run_id = run["counts"]["propagation_run_id"]
        assert run["status"] == "applying"
        assert (await repo.read_run(conn, child_run_id))["status"] == "applying"
        before = [
            e for child in children for e in await repo.events_for_entry(conn, "decisions", child)
        ]
        assert len(before) == 1
    monkeypatch.setattr(pipeline, "_propagation_entry", original)
    result = await pipeline.resume(run_id, BackupProof("resume", clock.now()))
    assert result.status == "applied"
    async with private_head_engine.connect() as conn:
        assert (await repo.read_run(conn, child_run_id))["status"] == "applied"
        after = [
            e for child in children for e in await repo.events_for_entry(conn, "decisions", child)
        ]
        assert len(after) == 2 and before[0]["id"] in {e["id"] for e in after}
        assert len(await repo.events_for_entry(conn, "decisions", parent)) == 1
        assert await repo.cache_mismatches(conn) == []


async def test_lifecycle_dry_run_has_no_event_or_cache(private_head_engine: AsyncEngine) -> None:
    project, entity_id = await seed(private_head_engine)
    clock = Clock()
    pipeline = LifecyclePipeline(
        private_head_engine,
        Client(),
        SourceProvider(),
        clock,
        Gates(),
        commit_gate=fake_commit_gate(clock),
    )
    report = await pipeline.start("dry_run", {"project_key": project}, 1)
    async with private_head_engine.connect() as conn:
        for table in (t.knowledge_extractions, t.knowledge_evaluations):
            assert (
                await conn.scalar(
                    sa.select(sa.func.count())
                    .select_from(table)
                    .where(
                        table.c.entity_id == entity_id,
                    )
                )
                == 1
            )
        for table in (t.knowledge_lifecycle_events, t.knowledge_lifecycle_state):
            assert (
                await conn.scalar(
                    sa.select(sa.func.count())
                    .select_from(table)
                    .where(
                        table.c.entity_id == entity_id,
                    )
                )
                == 0
            )
        assert (await PgKnowledgeLifecycleRepo().read_run(conn, report.run_id))[
            "status"
        ] == "applied"


async def test_lifecycle_two_days_compensation_replay_and_cache(
    private_head_engine: AsyncEngine,
) -> None:
    project, entity_id = await seed(private_head_engine)
    clock = Clock()
    pipeline = LifecyclePipeline(
        private_head_engine,
        Client(),
        SourceProvider(),
        clock,
        Gates(),
        commit_gate=fake_commit_gate(clock),
    )
    scope = {"project_key": project}
    first = await pipeline.start("apply", scope, 1, BackupProof("b", clock.now()))
    async with private_head_engine.connect() as conn:
        repo = PgKnowledgeLifecycleRepo()
        assert await repo.read_cache(conn, "decisions", entity_id) is None
    clock.at += timedelta(days=1)
    second = await pipeline.start("apply", scope, 1, BackupProof("b", clock.now()))
    async with private_head_engine.connect() as conn:
        assert (await repo.read_cache(conn, "decisions", entity_id))["state"] == "refuted"
        # The additional repository read is necessary to replay hysteresis on invalidation.
        history = await repo.evaluations_for_entry(conn, "decisions", entity_id)
        assert [e["run_id"] for e in history] == [first.run_id, second.run_id]
        assert all(not e["invalidated"] and e["mode"] == "apply" for e in history)
    await pipeline.resume(second.run_id, BackupProof("b", clock.now()))
    async with private_head_engine.connect() as conn:
        assert len(await repo.events_for_entry(conn, "decisions", entity_id)) == 1
    await pipeline.invalidate(second.run_id, "source error", BackupProof("b", clock.now()))
    await pipeline.invalidate(second.run_id, "retry", BackupProof("b", clock.now()))
    async with private_head_engine.connect() as conn:
        events = await repo.events_for_entry(conn, "decisions", entity_id)
        assert len(events) == 2 and events[-1]["kind"] == "compensation"
        assert events[-1]["compensates_event_id"] == events[0]["id"]
        assert (await repo.read_cache(conn, "decisions", entity_id))["state"] == "valid"
        assert await repo.cache_mismatches(conn) == []
        history = await repo.evaluations_for_entry(conn, "decisions", entity_id)
        assert history[-1]["invalidated"] is True


async def test_lifecycle_structural_obsolete_propagates_and_compensates_on_postgres(
    private_head_engine: AsyncEngine,
) -> None:
    project, parent = await seed(private_head_engine)
    _, child = await seed(private_head_engine)
    _, successor = await seed(private_head_engine)
    async with private_head_engine.begin() as conn:
        await conn.execute(
            t.decisions.update()
            .where(t.decisions.c.id == parent)
            .values(status="superseded", superseded_by=successor)
        )
        graph_ids = dict(
            (
                await conn.execute(
                    sa.select(t.brain_entities.c.source_uuid, t.brain_entities.c.id).where(
                        t.brain_entities.c.entity_type == "decision",
                        t.brain_entities.c.source_uuid.in_((parent, child)),
                    )
                )
            ).all()
        )
        await conn.execute(
            t.entity_relations.insert().values(
                source_entity_id=graph_ids[child],
                target_entity_id=graph_ids[parent],
                relation_type="DEPENDS_ON",
                origin="test",
            )
        )
    clock = Clock()
    pipeline = LifecyclePipeline(
        private_head_engine,
        Client(),
        SourceProvider(),
        clock,
        Gates(),
        commit_gate=fake_commit_gate(clock),
    )
    result = await pipeline.start(
        "apply", {"project_key": project}, 1, BackupProof("b", clock.now())
    )
    repo = PgKnowledgeLifecycleRepo()
    async with private_head_engine.connect() as conn:
        assert (await repo.read_cache(conn, "decisions", parent))["state"] == "obsolete"
        assert (await repo.read_cache(conn, "decisions", child))["state"] == "contested"
        run = await repo.read_run(conn, result.run_id)
        child_run = await repo.read_run(conn, run["counts"]["propagation_run_id"])
        assert child_run["scope"] == {"propagation_for": result.run_id}
        assert await repo.entries_for_run(conn, child_run["run_id"]) == [("decisions", child)]
    result = await pipeline.invalidate(
        result.run_id, "bad structure", BackupProof("b", clock.now())
    )
    assert result.status == "invalidated" and result.failure_reason is None
    async with private_head_engine.connect() as conn:
        assert (await repo.read_cache(conn, "decisions", parent))["state"] == "valid"
        assert (await repo.read_cache(conn, "decisions", child))["state"] == "valid"
        assert (await repo.events_for_entry(conn, "decisions", child))[-1]["kind"] == "compensation"
        assert await repo.cache_mismatches(conn) == []


async def test_lifecycle_real_commit_gate_rolls_back_closed_window(
    private_head_engine: AsyncEngine,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    project, parent = await seed(private_head_engine)
    _, successor = await seed(private_head_engine)
    async with private_head_engine.begin() as conn:
        await conn.execute(
            t.decisions.update()
            .where(t.decisions.c.id == parent)
            .values(status="superseded", superseded_by=successor)
        )
    original = AsyncConnection.scalar

    async def closed(conn: AsyncConnection, statement: Any, *args: Any, **kwargs: Any) -> Any:
        if "clock_timestamp()" in str(statement):
            return datetime(2026, 10, 14, 1).time()
        return await original(conn, statement, *args, **kwargs)

    monkeypatch.setattr(AsyncConnection, "scalar", closed)
    clock = Clock()
    pipeline = LifecyclePipeline(private_head_engine, Client(), SourceProvider(), clock, Gates())
    result = await pipeline.start(
        "apply", {"project_key": project}, 1, BackupProof("b", clock.now())
    )
    assert result.status == "applying"
    async with private_head_engine.connect() as conn:
        assert await PgKnowledgeLifecycleRepo().events_for_entry(conn, "decisions", parent) == []
