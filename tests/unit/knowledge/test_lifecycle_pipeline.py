"""Exercise checkpoints and safety gates without external services."""

from contextlib import asynccontextmanager
from copy import deepcopy
from datetime import UTC, datetime, timedelta
from typing import Any
from uuid import uuid4

import pytest
from structlog.testing import capture_logs

from brain_v42.knowledge import extraction, facts_registry
from brain_v42.knowledge.checks import Sources
from brain_v42.repositories.pg_knowledge_lifecycle import (
    ApplyDeferred,
    ApplyWindowClosed,
    LockedApplyInputs,
    StaleAtApply,
)
from brain_v42.services.knowledge_lifecycle_pipeline import (
    BackupProof,
    EvaluationSources,
    LifecyclePipeline,
    PipelineRefusal,
)

NOW = datetime(2026, 10, 14, 12, tzinfo=UTC)


class Clock:
    def __init__(self) -> None:
        self.at = NOW

    def now(self) -> datetime:
        return self.at


class Gates:
    def __init__(self) -> None:
        self.calls = 0
        self.refusal: str | None = None

    async def check(self, mode: str) -> str | None:
        self.calls += 1
        return self.refusal


class SourceProvider:
    def __init__(self) -> None:
        self.calls = []
        self.failed = frozenset()

    async def snapshot(self, day: Any) -> EvaluationSources:
        self.calls.append(day)
        return EvaluationSources(
            Sources(
                frozenset(),
                frozenset(),
                {},
                {},
                {"ci.system": "github-actions"},
                facts_registry.REGISTRY,
                "a" * 40,
            ),
            self.failed,
        )


class Client:
    def __init__(self) -> None:
        self.submissions = 0
        self.collections = 0
        self.cost = 0.01
        self.manifest: extraction.Manifest | None = None
        self.status = extraction.ResultStatus.SUCCEEDED

    async def count(self, manifest: extraction.Manifest) -> extraction.TokenCounts:
        self.manifest = manifest
        return extraction.TokenCounts(
            extraction.manifest_digest(manifest),
            {r.custom_id: 100 for r in manifest.requests},
            self.cost,
        )

    async def submit(self, manifest: extraction.Manifest) -> str:
        self.submissions += 1
        self.manifest = manifest
        return "batch-one"

    async def collect(
        self, batch_id: str, manifest: extraction.Manifest
    ) -> extraction.CollectedResults:
        self.collections += 1
        claim: extraction.Assertion = {
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
                    self.status,
                    tuple(
                        extraction.Extraction(e.entity_type, e.entity_id, e.digest, (claim,))
                        for e in r.entries
                    )
                    if self.status == "succeeded"
                    else (),
                    extraction.Usage(input_tokens=100, output_tokens=20),
                )
                for r in manifest.requests
            }
        )


class MemoryRepo:
    def __init__(self) -> None:
        self.runs: dict[str, Any] = {}
        self.requests: dict[tuple[str, str], Any] = {}
        self.extractions: dict[tuple[Any, ...], Any] = {}
        self.evaluations: list[Any] = []
        self.observations: dict[int, Any] = {}
        self.events: list[Any] = []
        self.cache: dict[tuple[Any, ...], Any] = {}
        self.entries: list[Any] = []
        self.edges: dict[tuple[Any, ...], list[Any]] = {}
        self.defer: Exception | None = None
        self.locked = False
        self.commits = 0
        self.after_apply: Any = None

    def add_entry(self, **extra: Any) -> dict[str, Any]:
        row = {
            "entity_type": "decisions",
            "id": uuid4(),
            "project_key": "brain-v42",
            "created_at": NOW,
            "content_updated_at": NOW,
            "text_fields": {"title": "gitlab-ci"},
            "successor_exists": False,
            "freshness_status": "fresh",
            "merged_into": None,
        }
        row.update(extra)
        self.entries.append(row)
        return row

    async def create_run(self, conn: Any, run_id: str, mode: str, scope: Any) -> Any:
        row = {
            "run_id": run_id,
            "mode": mode,
            "scope": scope,
            "status": "created",
            "batch_ids": [],
            "counts": {},
            "usage": {},
            "real_cost": None,
            "started_at": NOW,
            "ended_at": None,
            "invalidated_at": None,
        }
        self.runs[run_id] = row
        return deepcopy(row)

    async def read_run(self, conn: Any, run_id: str) -> Any:
        return deepcopy(self.runs[run_id])

    async def transition_run(
        self, conn: Any, run_id: str, source: Any, target: Any, **kwargs: Any
    ) -> Any:
        assert self.runs[run_id]["status"] == source
        self.runs[run_id]["status"] = str(target)
        if "reason" in kwargs:
            self.runs[run_id]["counts"]["failure_reason"] = kwargs["reason"]
        if "manifest_digest" in kwargs:
            self.runs[run_id]["manifest_digest"] = kwargs["manifest_digest"]
        return await self.read_run(conn, run_id)

    async def record_run(self, conn: Any, run_id: str, **kwargs: Any) -> Any:
        row = self.runs[run_id]
        for key, value in kwargs.items():
            if key == "batch_id":
                if value not in row["batch_ids"]:
                    row["batch_ids"].append(value)
            elif key in {"usage", "counts"}:
                row[key].update(deepcopy(value))
            else:
                row[key] = value
        return await self.read_run(conn, run_id)

    async def set_count_key(
        self, conn: Any, run_id: str, section: str, key: str, value: Any
    ) -> None:
        nested = self.runs[run_id]["counts"].setdefault(section, {})
        if value is None:
            nested.pop(key, None)
        else:
            nested[key] = deepcopy(value)

    async def list_entries(self, conn: Any, project_key: Any = None) -> Any:
        return deepcopy(
            [r for r in self.entries if project_key is None or r["project_key"] == project_key]
        )

    async def insert_requests(self, conn: Any, run_id: str, requests: Any) -> None:
        for cid, manifest in requests.items():
            self.requests[(run_id, cid)] = {
                "custom_id": cid,
                "manifest_entry": deepcopy(manifest),
                "status": "pending",
                "result_stored_at": None,
            }

    async def pending_requests(self, conn: Any, run_id: str) -> Any:
        return [
            deepcopy(r)
            for (rid, _), r in self.requests.items()
            if rid == run_id and r["result_stored_at"] is None
        ]

    async def checkpoint_request(self, conn: Any, run_id: str, cid: str, status: str) -> bool:
        row = self.requests[(run_id, cid)]
        if row["result_stored_at"] is not None:
            return False
        row.update(status=str(status), result_stored_at=NOW)
        return True

    async def read_extraction(
        self, conn: Any, kind: str, eid: Any, digest: str, **kwargs: Any
    ) -> Any:
        return self.extractions.get((kind, eid, digest))

    async def insert_extraction(self, conn: Any, **values: Any) -> int:
        key = values["entity_type"], values["entity_id"], values["entry_digest"]
        row = self.extractions.setdefault(key, dict(id=len(self.extractions) + 1, **values))
        return row["id"]

    async def evaluations_for_entry(self, conn: Any, kind: str, eid: Any) -> Any:
        return [
            dict(
                e,
                mode=self.runs[e["run_id"]]["mode"],
                run_status=self.runs[e["run_id"]]["status"],
                invalidated=self.runs[e["run_id"]]["status"] == "invalidated",
            )
            for e in self.evaluations
            if (e["entity_type"], e["entity_id"]) == (kind, eid)
        ]

    async def insert_evaluation(self, conn: Any, values: Any, observations: Any) -> Any:
        for e in self.evaluations:
            if (e["run_id"], e["entity_type"], e["entity_id"]) == (
                values["run_id"],
                values["entity_type"],
                values["entity_id"],
            ):
                return e
        row = dict(id=len(self.evaluations) + 1, **values)
        self.evaluations.append(row)
        self.observations[row["id"]] = deepcopy(observations)
        return row

    async def read_evaluation(self, conn: Any, eid: int) -> Any:
        return deepcopy(self.evaluations[eid - 1])

    async def read_justifications(self, conn: Any, eid: int) -> Any:
        return deepcopy(self.observations[eid])

    async def read_cache(self, conn: Any, kind: str, eid: Any) -> Any:
        return deepcopy(self.cache.get((kind, eid)))

    async def lock_apply_inputs(self, conn: Any, kind: str, eid: Any) -> Any:
        if self.defer:
            raise self.defer
        row = next(r for r in self.entries if (r["entity_type"], r["id"]) == (kind, eid))
        return LockedApplyInputs(kind, eid, deepcopy(row), await self.read_cache(conn, kind, eid))

    async def append_transition(self, conn: Any, eid: int, inputs: Any, **values: Any) -> Any:
        prior = next(
            (e for e in self.events if e["evaluation_id"] == eid and e["kind"] == "transition"),
            None,
        )
        if prior:
            return prior
        assert inputs.revision == self.evaluations[eid - 1]["expected_cache_revision"]
        return self._event(eid, inputs, "transition", values)

    def _event(self, eid: int, inputs: Any, kind: str, values: Any) -> Any:
        row = dict(
            id=len(self.events) + 1,
            evaluation_id=eid,
            kind=kind,
            entity_type=inputs.entity_type,
            entity_id=inputs.entity_id,
            from_state=None if inputs.cache is None else inputs.cache["state"],
            **values,
        )
        self.events.append(row)
        self.cache[(inputs.entity_type, inputs.entity_id)] = {
            "state": values["to_state"],
            "reason": values["reason"],
            "event_id": row["id"],
            "revision": inputs.revision + 1,
        }
        if self.after_apply:
            self.after_apply()
        return row

    async def append_compensation(
        self, conn: Any, *, evaluation_id: int, inputs: Any, **values: Any
    ) -> Any:
        prior = next(
            (
                e
                for e in self.events
                if e.get("invalidated_run_id") == values["invalidated_run_id"]
                and e["entity_id"] == inputs.entity_id
            ),
            None,
        )
        if prior:
            return prior
        return self._event(evaluation_id, inputs, "compensation", values)

    async def invalidate_run(self, conn: Any, run_id: str, reason: str) -> bool:
        row = self.runs[run_id]
        changed = row["status"] != "invalidated"
        row.update(status="invalidated", invalidated_at=NOW)
        return changed

    async def entries_for_run(self, conn: Any, run_id: str) -> Any:
        return [
            (e["entity_type"], e["entity_id"]) for e in self.evaluations if e["run_id"] == run_id
        ]

    async def events_for_entry(self, conn: Any, kind: str, eid: Any) -> Any:
        return [e for e in self.events if (e["entity_type"], e["entity_id"]) == (kind, eid)]

    async def compensations_for_run(self, conn: Any, run_id: str) -> Any:
        return [e for e in self.events if e.get("invalidated_run_id") == run_id]

    async def direct_dependents(self, conn: Any, kind: str, eid: Any) -> Any:
        return self.edges.get((kind, eid), [])


class Engine:
    def __init__(self, repo: MemoryRepo) -> None:
        self.repo = repo

    @asynccontextmanager
    async def begin(self) -> Any:
        before = deepcopy(self.repo.__dict__)
        try:
            yield self
        except BaseException:
            self.repo.__dict__.update(before)
            raise
        else:
            self.repo.commits += 1


@pytest.fixture
def harness() -> Any:
    repo, client, sources, clock, gates = MemoryRepo(), Client(), SourceProvider(), Clock(), Gates()

    @asynccontextmanager
    async def lock(engine: Any) -> Any:
        if repo.locked:
            yield False
            return
        repo.locked = True
        try:
            yield True
        finally:
            repo.locked = False

    async def commit_gate(conn: Any) -> None:
        if clock.at.hour < 4 and clock.at.time().minute >= 50:
            raise ApplyWindowClosed("apply_window_closed")

    pipeline = LifecyclePipeline(
        Engine(repo),
        client,
        sources,
        clock,
        gates,
        repo=repo,
        run_lock=lock,
        commit_gate=commit_gate,
    )
    return pipeline, repo, client, sources, clock, gates


def proof(clock: Clock) -> BackupProof:
    return BackupProof("declared-backup", clock.now())


async def test_lifecycle_start_preserves_supplied_run_id(harness: Any) -> None:
    pipeline, repo, _, _, _, _ = harness
    report = await pipeline.start("dry_run", {}, 1, run_id="operator-run")
    assert report.run_id == "operator-run"
    assert set(repo.runs) == {"operator-run"}
    assert (await pipeline.report("operator-run")).status == report.status


async def test_lifecycle_start_generates_run_id_by_default(harness: Any) -> None:
    pipeline, repo, _, _, _, _ = harness
    report = await pipeline.start("dry_run", {}, 1)
    assert report.run_id.startswith("lifecycle-") and report.run_id in repo.runs


async def test_lifecycle_cli_reports_checkpoint_when_start_raises(
    harness: Any, monkeypatch: pytest.MonkeyPatch
) -> None:
    import io
    import json
    from unittest.mock import AsyncMock

    from brain_v42.config import Settings
    from brain_v42.lifecycle_cli.command import execute

    pipeline, repo, _, _, _, _ = harness
    monkeypatch.setattr(pipeline, "_drive", AsyncMock(side_effect=RuntimeError("interrupted")))
    output = io.StringIO()
    code = await execute(
        ["run", "--mode", "dry-run", "--scope", "all", "--ceiling-usd", "1", "--json"],
        Settings(
            postgres_url="postgresql+asyncpg://unused/unused",
            _env_file=None,
            brain_knowledge_lifecycle_enabled=True,
        ),
        pipeline,
        stdout=output,
    )
    result = json.loads(output.getvalue())
    assert code == 3 and result["failure_reason"] == "command_failed"
    assert result["run_id"] in repo.runs and result["status"] == "created"
    assert (await pipeline.report(result["run_id"])).run_id == result["run_id"]


@pytest.mark.parametrize("discard", [False, True], ids=["collect", "discard"])
async def test_lifecycle_request_status_updates_only_one_key(
    harness: Any, monkeypatch: pytest.MonkeyPatch, discard: bool
) -> None:
    pipeline, repo, _, _, _, _ = harness
    for project in ("first", "second"):
        repo.add_entry(project_key=project)
    run_id = "status-checkpoint"
    await repo.create_run(None, run_id, "dry_run", {})
    await repo.record_run(None, run_id, counts={"ceiling_usd": 1})
    await pipeline._freeze(await pipeline._read_run(run_id))
    original_record, original_set = repo.record_run, repo.set_count_key
    updated = []

    async def record_one(conn: Any, rid: str, **kwargs: Any) -> Any:
        assert "request_statuses" not in kwargs.get("counts", {})
        return await original_record(conn, rid, **kwargs)

    async def set_one(conn: Any, rid: str, section: str, key: str, value: Any) -> None:
        if section == "request_statuses":
            updated.append(key)
        await original_set(conn, rid, section, key, value)

    monkeypatch.setattr(repo, "record_run", record_one)
    monkeypatch.setattr(repo, "set_count_key", set_one)
    if discard:
        repo.runs[run_id]["status"] = "submitting"
        await pipeline.resume(run_id)
        report = await pipeline.discard_failed_batch(run_id, "found-later")
    else:
        await pipeline._submit(await pipeline._read_run(run_id), None)
        await pipeline._collect(await pipeline._read_run(run_id))
        report = await pipeline.report(run_id)
    assert len(updated) == 2 and set(updated) == set(report.request_statuses)
    assert set(report.request_statuses.values()) == {"succeeded"}
    assert report.usage["input_tokens"] == 200 and report.real_cost_usd > 0


async def test_lifecycle_resume_preserves_initial_apply_backup(harness: Any) -> None:
    pipeline, repo, _, _, clock, _ = harness
    repo.add_entry(merged_into=uuid4())
    clock.at = NOW.replace(hour=0, minute=45)
    supplied = BackupProof("apply-backup", clock.now())
    report = await pipeline.start("apply", {}, 1, supplied)
    assert report.status == "applying"
    clock.at = NOW.replace(hour=4)
    report = await pipeline.resume(report.run_id, BackupProof("resume-backup", clock.now()))
    assert report.status == "applied"
    assert report.backup_run_id == supplied.run_id
    assert report.backup_sealed_at == supplied.sealed_at.isoformat()
    assert repo.runs[report.run_id]["counts"]["backup_apply"]["run_id"] == supplied.run_id


@pytest.mark.parametrize("status", ["applied", "failed", "invalidated"])
@pytest.mark.parametrize("supplied", ["missing", "fresh", "expired"])
async def test_lifecycle_terminal_resume_neither_requires_nor_writes_backup(
    harness: Any, monkeypatch: pytest.MonkeyPatch, status: str, supplied: str
) -> None:
    pipeline, repo, _, _, clock, _ = harness
    await repo.create_run(None, "terminal", "apply", {})
    repo.runs["terminal"].update(
        status=status,
        counts={
            "backup_apply": {"run_id": "apply-backup", "sealed_at": NOW.isoformat()},
            "backup_invalidate": {"run_id": "invalidate-backup", "sealed_at": NOW.isoformat()},
        },
    )
    before = deepcopy(repo.runs["terminal"])

    async def no_write(*args: Any, **kwargs: Any) -> Any:
        pytest.fail("A terminal resume must not write backup metadata")

    monkeypatch.setattr(repo, "record_run", no_write)
    backup = None
    if supplied != "missing":
        age = timedelta(hours=2) if supplied == "expired" else timedelta()
        backup = BackupProof("resume-backup", clock.now() - age)
    report = await pipeline.resume("terminal", backup)
    assert report.status == status
    assert report.backup_run_id == (
        "invalidate-backup" if status == "invalidated" else "apply-backup"
    )
    assert repo.runs["terminal"] == before


async def test_lifecycle_invalidation_keeps_backup_proofs_per_phase(harness: Any) -> None:
    pipeline, repo, _, _, clock, _ = harness
    applied = await pipeline.start("apply", {}, 1, BackupProof("apply-backup", clock.now()))
    result = await pipeline.invalidate(
        applied.run_id, "bad source", BackupProof("invalidate-backup", clock.now())
    )
    counts = repo.runs[applied.run_id]["counts"]
    assert counts["backup_apply"]["run_id"] == applied.backup_run_id == "apply-backup"
    assert counts["backup_invalidate"]["run_id"] == result.backup_run_id == "invalidate-backup"
    assert result.backup_sealed_at == clock.now().isoformat()


async def test_lifecycle_dry_run_persists_evidence_without_state(harness: Any) -> None:
    pipeline, repo, client, _, _, _ = harness
    repo.add_entry()
    report = await pipeline.start("dry_run", {"project_key": "brain-v42"}, 1)
    assert report.status == "applied"
    assert len(repo.evaluations) == len(repo.extractions) == 1
    assert not repo.events and not repo.cache
    assert report.pending_by_state == {"refuted": 1}
    assert report.spot_checks == [str(repo.entries[0]["id"])]
    assert report.real_cost_usd > 0
    assert report.to_json() and client.submissions == 1


async def test_lifecycle_two_days_then_invalidation_and_replay(harness: Any) -> None:
    pipeline, repo, client, _, clock, _ = harness
    row = repo.add_entry()
    first = await pipeline.start("apply", {}, 1, proof(clock))
    assert not repo.events
    same_day = await pipeline.start("apply", {}, 1, proof(clock))
    assert not repo.events and same_day.pending_by_state == {"refuted": 1}
    clock.at += timedelta(days=1)
    second = await pipeline.start("apply", {}, 1, proof(clock))
    assert repo.cache[("decisions", row["id"])]["state"] == "refuted"
    assert client.submissions == 1
    await pipeline.resume(second.run_id, proof(clock))
    assert len(repo.events) == 1
    await pipeline.invalidate(second.run_id, "wrong source", proof(clock))
    assert repo.cache[("decisions", row["id"])]["state"] == "valid"
    assert repo.events[-1]["kind"] == "compensation"
    await pipeline.invalidate(second.run_id, "retry", proof(clock))
    assert len(repo.events) == 2
    assert first.backup_proof == "backup proof declared by the coordination, not re-read"


@pytest.mark.parametrize("age", [None, 3601, -1])
async def test_lifecycle_backup_refuses_before_applying(harness: Any, age: Any) -> None:
    pipeline, repo, _, _, clock, _ = harness
    supplied = None if age is None else BackupProof("b", clock.now() - timedelta(seconds=age))
    with pytest.raises(PipelineRefusal, match="backup"):
        await pipeline.start("apply", {}, 1, supplied)
    assert next(iter(repo.runs.values()))["status"] == "evaluated"
    assert not repo.events


async def test_lifecycle_guard_fails_before_submit(harness: Any) -> None:
    pipeline, repo, client, _, _, _ = harness
    repo.add_entry()
    report = await pipeline.start("dry_run", {}, 0.001)
    assert report.status == "failed" and report.failure_reason == "cost_guard_refused"
    assert client.submissions == 0


async def test_lifecycle_lock_and_identity_refusals(harness: Any) -> None:
    pipeline, repo, _, _, _, gates = harness
    repo.locked = True
    with pytest.raises(PipelineRefusal, match="run_locked"):
        await pipeline.start("dry_run", {}, 1)
    repo.locked = False
    gates.refusal = "wrong_target"
    with pytest.raises(PipelineRefusal, match="wrong_target"):
        await pipeline.start("dry_run", {}, 1)
    assert not repo.runs


async def test_lifecycle_ambiguous_submission_never_resubmits(harness: Any) -> None:
    pipeline, repo, client, _, _, _ = harness
    await repo.create_run(None, "ambiguous", "dry_run", {})
    repo.runs["ambiguous"]["status"] = "submitting"
    report = await pipeline.resume("ambiguous")
    assert report.status == "failed" and report.failure_reason == "ambiguous_submission"
    assert client.submissions == 0


@pytest.mark.parametrize(
    "error,cause",
    [
        (StaleAtApply("changed"), "stale_at_apply"),
        (ApplyDeferred("55P03"), "lock_timeout"),
        (ApplyDeferred("57014"), "query_canceled"),
    ],
)
async def test_lifecycle_deferrals_do_not_fail_run(harness: Any, error: Any, cause: str) -> None:
    pipeline, repo, _, _, clock, _ = harness
    repo.add_entry(merged_into=uuid4())
    repo.defer = error
    report = await pipeline.start("apply", {}, 1, proof(clock))
    assert report.status == "applied" and report.deferred_by_cause == {cause: 1}
    assert not repo.events


async def test_lifecycle_failed_check_cannot_confirm(harness: Any) -> None:
    pipeline, repo, _, sources, clock, _ = harness
    repo.add_entry()
    await pipeline.start("apply", {}, 1, proof(clock))
    # An auxiliary failed check emits no evidence on either day. The decisive
    # observation, digest and fingerprint stay identical across the two days.
    stored = next(iter(repo.extractions.values()))
    auxiliary = dict(
        stored["assertions"][0], assertion_key="auxiliary", fact_key="ci.runner", ref="ci.runner"
    )
    stored["assertions"] = (*stored["assertions"], auxiliary)
    clock.at += timedelta(days=1)
    sources.failed = frozenset({"fact:ci.runner"})
    await pipeline.start("apply", {}, 1, proof(clock))
    assert repo.evaluations[-1]["complete"] is False
    assert repo.evaluations[0]["fingerprint"] == repo.evaluations[-1]["fingerprint"]
    assert not repo.events


async def test_lifecycle_clock_stops_as_applying_and_requires_fresh_resume_proof(
    harness: Any,
) -> None:
    pipeline, repo, _, _, clock, _ = harness
    repo.add_entry(merged_into=uuid4())
    clock.at = NOW.replace(hour=0, minute=45)
    report = await pipeline.start("apply", {}, 1, proof(clock))
    assert report.status == "applying" and not repo.events
    assert len(repo.evaluations) == 1
    old_proof = proof(clock)
    clock.at = NOW.replace(hour=4)
    with pytest.raises(PipelineRefusal, match="backup"):
        await pipeline.resume(report.run_id, old_proof)
    report = await pipeline.resume(report.run_id, proof(clock))
    assert report.status == "applied" and len(repo.events) == 1
    assert len(repo.evaluations) == 1


async def test_lifecycle_chunk_gate_rechecked_after_200_entries(harness: Any) -> None:
    pipeline, repo, _, _, clock, gates = harness
    for _ in range(201):
        repo.add_entry(merged_into=uuid4())

    def close_gate() -> None:
        if len(repo.events) == 200:
            gates.refusal = "killswitch_closed"

    repo.after_apply = close_gate
    with pytest.raises(PipelineRefusal, match="killswitch_closed"):
        await pipeline.start("apply", {}, 100, proof(clock))
    assert len(repo.events) == 200
    assert next(iter(repo.runs.values()))["status"] == "applying"


async def test_lifecycle_precommit_clock_rolls_back_event(harness: Any) -> None:
    pipeline, repo, _, _, clock, _ = harness
    repo.add_entry(merged_into=uuid4())
    clock.at = NOW.replace(hour=0, minute=44)
    repo.after_apply = lambda: setattr(clock, "at", clock.at.replace(minute=50))
    report = await pipeline.start("apply", {}, 1, proof(clock))
    assert report.status == "applying" and not repo.events and not repo.cache


async def test_lifecycle_propagation_is_one_hop_and_compensates(harness: Any) -> None:
    pipeline, repo, _, _, clock, _ = harness
    parent = repo.add_entry(merged_into=uuid4(), project_key="parent")
    child = repo.add_entry(project_key="child")
    grandchild = repo.add_entry(project_key="grandchild")
    repo.edges[("decisions", parent["id"])] = [("decisions", child["id"])]
    repo.edges[("decisions", child["id"])] = [("decisions", grandchild["id"])]
    report = await pipeline.start("apply", {"project_key": "parent"}, 1, proof(clock))
    assert repo.cache[("decisions", child["id"])]["state"] == "contested"
    assert ("decisions", grandchild["id"]) not in repo.cache
    await pipeline.invalidate(report.run_id, "bad structure", proof(clock))
    assert repo.cache[("decisions", parent["id"])]["state"] == "valid"
    assert repo.cache[("decisions", child["id"])]["state"] == "valid"


async def test_lifecycle_report_never_contains_screened_text(harness: Any) -> None:
    pipeline, repo, client, _, _, _ = harness
    # A credential marker with inert synthetic characters exercises the screen.
    marker = "-----BEGIN PRIVATE KEY-----"
    row = repo.add_entry(text_fields={"title": marker}, merged_into=uuid4())
    with capture_logs() as logs:
        report = await pipeline.start("dry_run", {}, 1)
    assert report.screened_entries == [str(row["id"])]
    assert marker not in report.to_json()
    assert marker not in repr(logs)
    assert client.submissions == 0 and not repo.extractions
    assert repo.evaluations[0]["state"] == "obsolete"


@pytest.mark.parametrize("status", ["errored", "expired", "canceled", "missing", "invalid"])
async def test_lifecycle_unsuccessful_requests_leave_no_extraction(
    harness: Any, status: str
) -> None:
    pipeline, repo, client, _, _, _ = harness
    repo.add_entry()
    client.status = extraction.ResultStatus(status)
    report = await pipeline.start("dry_run", {}, 1)
    assert not repo.extractions and repo.evaluations[0]["complete"] is False
    assert list(report.request_statuses.values()) == [status]


async def test_lifecycle_invalidation_preserves_another_applied_parent(harness: Any) -> None:
    pipeline, repo, _, _, clock, _ = harness
    parent = repo.add_entry(merged_into=uuid4(), project_key="parent")
    other = repo.add_entry(merged_into=uuid4(), project_key="other")
    child = repo.add_entry(project_key="child")
    for row in (parent, other):
        repo.edges[("decisions", row["id"])] = [("decisions", child["id"])]
    first = await pipeline.start("apply", {"project_key": "parent"}, 1, proof(clock))
    await pipeline.start("apply", {"project_key": "other"}, 1, proof(clock))
    await pipeline.invalidate(first.run_id, "bad parent", proof(clock))
    cache = repo.cache[("decisions", child["id"])]
    assert cache["state"] == "contested"
    assert cache["reason"] == f"propagated_from_{other['id']}"


async def test_lifecycle_structural_input_changed_before_apply_defers(harness: Any) -> None:
    pipeline, repo, _, _, clock, _ = harness
    row = repo.add_entry(merged_into=uuid4())
    clock.at = NOW.replace(hour=0, minute=45)
    report = await pipeline.start("apply", {}, 1, proof(clock))
    row["merged_into"] = None
    clock.at = NOW.replace(hour=4)
    report = await pipeline.resume(report.run_id, proof(clock))
    assert report.deferred_by_cause == {"stale_at_apply": 1}
    assert not repo.events


async def test_lifecycle_digest_change_resets_confirmation(harness: Any) -> None:
    pipeline, repo, client, _, clock, _ = harness
    row = repo.add_entry()
    await pipeline.start("apply", {}, 1, proof(clock))
    clock.at += timedelta(days=1)
    row["text_fields"]["description"] = "new revision"
    row["content_updated_at"] = clock.at
    await pipeline.start("apply", {}, 1, proof(clock))
    assert client.submissions == 2 and not repo.events


async def test_lifecycle_request_checkpoint_skips_usage_and_extraction_on_resume(
    harness: Any,
) -> None:
    pipeline, repo, client, sources, _, _ = harness
    repo.add_entry()

    async def unavailable(day: Any) -> Any:
        raise RuntimeError("measurement unavailable")

    original = sources.snapshot
    sources.snapshot = unavailable
    with pytest.raises(RuntimeError, match="measurement unavailable"):
        await pipeline.start("dry_run", {}, 1)
    run_id = next(iter(repo.runs))
    assert repo.runs[run_id]["status"] == "results_stored"
    cost = repo.runs[run_id]["real_cost"]
    sources.snapshot = original
    await pipeline.resume(run_id)
    assert client.collections == 1 and client.submissions == 1
    assert len(repo.extractions) == 1 and repo.runs[run_id]["real_cost"] == cost


async def test_lifecycle_batch_wait_leaves_submitted_without_checkpoint(
    harness: Any, monkeypatch: pytest.MonkeyPatch
) -> None:
    from unittest.mock import AsyncMock

    from brain_v42.lifecycle_cli.adapters import BatchStillProcessing

    pipeline, repo, client, sources, _, _ = harness
    repo.add_entry()
    original = client.collect
    monkeypatch.setattr(
        client, "collect", AsyncMock(side_effect=BatchStillProcessing("batch-one", 60))
    )
    with pytest.raises(BatchStillProcessing):
        await pipeline.start("dry_run", {}, 1, run_id="waiting")
    before = deepcopy(repo.runs["waiting"])
    assert before["status"] == "submitted" and before["batch_ids"] == ["batch-one"]
    assert "failure_reason" not in before["counts"]
    assert before["usage"] == {} and before["real_cost"] is None
    assert set(before["counts"]["request_statuses"].values()) == {"pending"}
    assert all(
        r["result_stored_at"] is None and r["status"] == "pending" for r in repo.requests.values()
    )
    assert not repo.extractions and not repo.evaluations and not repo.events and not repo.cache
    assert not sources.calls and not repo.locked
    with pytest.raises(BatchStillProcessing):
        await pipeline.resume("waiting")
    assert repo.runs["waiting"] == before and client.submissions == 1
    monkeypatch.setattr(client, "collect", original)
    report = await pipeline.resume("waiting")
    assert report.status == "applied" and client.submissions == client.collections == 1
    assert len(repo.extractions) == len(repo.evaluations) == 1


@pytest.mark.parametrize(
    "status", ["created", "manifest_frozen", "submitted", "results_stored", "evaluated", "applying"]
)
async def test_lifecycle_resume_each_checkpoint(harness: Any, status: str) -> None:
    pipeline, repo, client, _, _, _ = harness
    repo.add_entry()
    run_id = "resume-" + uuid4().hex
    await repo.create_run(None, run_id, "dry_run", {})
    await repo.record_run(None, run_id, counts={"ceiling_usd": 1})
    if status != "created":
        await pipeline._freeze(await pipeline._read_run(run_id))
    if status in {"submitted", "results_stored", "evaluated", "applying"}:
        await pipeline._submit(await pipeline._read_run(run_id), None)
    if status in {"results_stored", "evaluated", "applying"}:
        await pipeline._collect(await pipeline._read_run(run_id))
    if status in {"evaluated", "applying"}:
        await pipeline._evaluate(await pipeline._read_run(run_id))
    if status == "applying":
        await pipeline._edge(run_id, "evaluated", "applying")
    assert repo.runs[run_id]["status"] == status
    report = await pipeline.resume(run_id)
    assert report.status == "applied"
    assert len(repo.evaluations) == len(repo.extractions) == 1
    assert client.submissions == client.collections == 1


async def test_lifecycle_resume_submitting_with_batch_id_collects_without_submit(
    harness: Any,
) -> None:
    pipeline, repo, client, _, _, _ = harness
    repo.add_entry()
    run_id = "known-batch"
    await repo.create_run(None, run_id, "dry_run", {})
    await repo.record_run(None, run_id, counts={"ceiling_usd": 1})
    await pipeline._freeze(await pipeline._read_run(run_id))
    repo.runs[run_id].update(status="submitting", batch_ids=["batch-one"])
    report = await pipeline.resume(run_id)
    assert report.status == "applied" and client.submissions == 0 and client.collections == 1


async def test_lifecycle_propagation_cap_refuses_before_event(
    harness: Any, monkeypatch: Any
) -> None:
    from brain_v42.knowledge.decide import Decision
    from brain_v42.knowledge.ranks import State
    from brain_v42.services import knowledge_lifecycle_pipeline as module

    pipeline, repo, _, _, clock, _ = harness
    parent = repo.add_entry(merged_into=uuid4(), project_key="parent")
    child = repo.add_entry(project_key="child")
    repo.edges[("decisions", parent["id"])] = [("decisions", child["id"])]
    original = module.decide

    def over_cap(observations: Any, day: Any) -> Any:
        if observations and observations[0].source_kind == "propagation":
            return Decision(State.REFUTED, "bad_propagation", (), layer="propagation")
        return original(observations, day)

    monkeypatch.setattr(module, "decide", over_cap)
    report = await pipeline.start("apply", {"project_key": "parent"}, 1, proof(clock))
    assert report.status == "failed" and report.failure_reason == "propagation_cap_exceeded"
    assert report.offending_ids == [str(child["id"])]
    assert all(e["entity_id"] != child["id"] for e in repo.events)


async def test_lifecycle_resume_failed_child_fails_parent(harness: Any) -> None:
    pipeline, repo, _, _, clock, _ = harness
    await repo.create_run(None, "parent", "apply", {})
    await repo.create_run(None, "child", "apply", {})
    repo.runs["parent"].update(status="applying", counts={"propagation_run_id": "child"})
    repo.runs["child"].update(
        status="failed",
        counts={"failure_reason": "propagation_cap_exceeded", "offending_ids": ["one", "two"]},
    )
    report = await pipeline.resume("parent", proof(clock))
    assert report.status == "failed"
    assert report.failure_reason == "propagation_cap_exceeded"
    assert report.offending_ids == ["one", "two"]
    assert not repo.events


async def test_lifecycle_empty_manifest_does_not_leave_ambiguous_checkpoint(harness: Any) -> None:
    pipeline, repo, client, _, _, _ = harness
    row = repo.add_entry()
    await pipeline.start("dry_run", {}, 1)
    run_id = "no-submission"
    await repo.create_run(None, run_id, "dry_run", {})
    await repo.record_run(None, run_id, counts={"ceiling_usd": 1})
    await pipeline._freeze(await pipeline._read_run(run_id))
    assert not repo.runs[run_id]["counts"]["manifest"]["requests"]
    original = repo.transition_run

    async def fail_submitted(conn: Any, rid: str, source: Any, target: Any, **kwargs: Any) -> Any:
        if rid == run_id and target == "submitted":
            raise RuntimeError("crash")
        return await original(conn, rid, source, target, **kwargs)

    repo.transition_run = fail_submitted
    with pytest.raises(RuntimeError, match="crash"):
        await pipeline._submit(await pipeline._read_run(run_id), None)
    assert repo.runs[run_id]["status"] == "manifest_frozen"
    assert client.submissions == 1 and row["id"] is not None


async def test_lifecycle_gates_rechecked_after_token_count(harness: Any) -> None:
    pipeline, repo, client, _, _, gates = harness
    repo.add_entry()
    original = client.count

    async def close_after_count(manifest: Any) -> Any:
        counted = await original(manifest)
        gates.refusal = "target_changed"
        return counted

    client.count = close_after_count
    with pytest.raises(PipelineRefusal, match="target_changed"):
        await pipeline.start("dry_run", {}, 1)
    assert client.submissions == 0
    assert next(iter(repo.runs.values()))["status"] == "manifest_frozen"


async def test_lifecycle_compares_structural_fields_without_an_emitted_observation(
    harness: Any,
) -> None:
    pipeline, repo, _, _, clock, _ = harness
    row = repo.add_entry(status="proposed")
    await pipeline.start("apply", {}, 1, proof(clock))
    clock.at = (clock.at + timedelta(days=1)).replace(hour=0, minute=45)
    report = await pipeline.start("apply", {}, 1, proof(clock))
    row["status"] = "active"
    clock.at = clock.at.replace(hour=4, minute=0)
    report = await pipeline.resume(report.run_id, proof(clock))
    assert report.deferred_by_cause == {"stale_at_apply": 1}
    assert not repo.events


async def test_lifecycle_resume_skips_a_committed_evaluation(harness: Any) -> None:
    pipeline, repo, _, _, _, _ = harness
    for _ in range(2):
        repo.add_entry()
    original = pipeline._save_snapshot
    calls = 0

    async def crash_second(conn: Any, rid: str, eid: int, row: Any) -> None:
        nonlocal calls
        calls += 1
        if calls == 2:
            raise RuntimeError("evaluation crash")
        await original(conn, rid, eid, row)

    pipeline._save_snapshot = crash_second
    with pytest.raises(RuntimeError, match="evaluation crash"):
        await pipeline.start("dry_run", {}, 1)
    run_id = next(iter(repo.runs))
    assert len(repo.evaluations) == 1
    pipeline._save_snapshot = original
    await pipeline.resume(run_id)
    assert len(repo.evaluations) == 2 and calls == 2


@pytest.mark.parametrize(
    "hour,minute,open_window", [(0, 44, True), (0, 45, False), (3, 59, False), (4, 0, True)]
)
async def test_lifecycle_chunk_window_boundaries(
    harness: Any, hour: int, minute: int, open_window: bool
) -> None:
    pipeline, repo, _, _, clock, _ = harness
    repo.add_entry(merged_into=uuid4())
    clock.at = NOW.replace(hour=hour, minute=minute)
    report = await pipeline.start("apply", {}, 1, proof(clock))
    assert (report.status == "applied") is open_window
    assert bool(repo.events) is open_window


async def test_lifecycle_guard_refusal_report_retains_counted_cost(harness: Any) -> None:
    pipeline, repo, client, _, _, _ = harness
    repo.add_entry()
    report = await pipeline.start("dry_run", {}, 0)
    assert report.worst_case_usd == client.cost
    assert list(report.token_counts.values()) == [100]


async def test_lifecycle_propagation_does_not_apply_pending_checks(harness: Any) -> None:
    pipeline, repo, _, _, clock, _ = harness
    parent = repo.add_entry(merged_into=uuid4())
    child = repo.add_entry()
    repo.edges[("decisions", parent["id"])] = [("decisions", child["id"])]
    report = await pipeline.start("apply", {}, 1, proof(clock))
    assert repo.cache[("decisions", child["id"])]["state"] == "contested"
    primary = [e for e in repo.evaluations if e["run_id"] == report.run_id]
    assert len(primary) == 2
    pending = next(e for e in primary if e["entity_id"] == child["id"])
    assert pending["state"] == "refuted"
    assert all(e["evaluation_id"] != pending["id"] for e in repo.events)


async def test_lifecycle_late_failed_batch_is_accounted_without_evidence(harness: Any) -> None:
    pipeline, repo, client, _, _, _ = harness
    repo.add_entry()
    run_id = "lost-submission"
    await repo.create_run(None, run_id, "dry_run", {})
    await repo.record_run(None, run_id, counts={"ceiling_usd": 1})
    await pipeline._freeze(await pipeline._read_run(run_id))
    repo.runs[run_id]["status"] = "submitting"
    await pipeline.resume(run_id)
    report = await pipeline.discard_failed_batch(run_id, "found-later")
    assert report.status == "failed" and report.real_cost_usd > 0
    assert not repo.extractions and not repo.evaluations and not repo.events
    cost = report.real_cost_usd
    report = await pipeline.discard_failed_batch(run_id, "found-later")
    assert report.real_cost_usd == cost and client.collections == 1


async def test_lifecycle_invalidating_dry_confirmation_compensates_later_apply(
    harness: Any,
) -> None:
    pipeline, repo, _, _, clock, _ = harness
    row = repo.add_entry()
    dry = await pipeline.start("dry_run", {}, 1)
    clock.at += timedelta(days=1)
    await pipeline.start("apply", {}, 1, proof(clock))
    assert repo.cache[("decisions", row["id"])]["state"] == "refuted"
    result = await pipeline.invalidate(dry.run_id, "bad confirmation", proof(clock))
    assert repo.cache[("decisions", row["id"])]["state"] == "valid"
    assert result.compensated_by_state == {"valid": 1}


async def test_lifecycle_invalidation_propagation_cap_refuses_before_dependent_event(
    harness: Any,
) -> None:
    from brain_v42.knowledge.decide import Decision
    from brain_v42.knowledge.ranks import State, ValidFromPrecision

    pipeline, repo, _, _, clock, _ = harness
    parent = repo.add_entry(merged_into=uuid4(), project_key="parent")
    child = repo.add_entry(project_key="child")
    repo.edges[("decisions", parent["id"])] = [("decisions", child["id"])]
    report = await pipeline.start("apply", {"project_key": "parent"}, 1, proof(clock))
    original = pipeline._propagation_decision

    async def over_cap(
        conn: Any, identity: Any, parents: Any, *, invalidating: bool = False
    ) -> Any:
        if identity[1] == child["id"]:
            return Decision(
                State.OBSOLETE, "bad_lift", (), clock.now().date(), ValidFromPrecision.EXACT
            )
        return await original(conn, identity, parents, invalidating=invalidating)

    pipeline._propagation_decision = over_cap
    recorded = await pipeline.invalidate(report.run_id, "bad structure", proof(clock))
    assert not any(
        e["entity_id"] == child["id"] and e["kind"] == "compensation" for e in repo.events
    )
    assert recorded.status == "invalidated"
    assert recorded.failure_reason == "propagation_cap_exceeded"
    assert recorded.offending_ids == [str(child["id"])]


async def test_lifecycle_report_does_not_label_an_unchanged_confirmed_state_pending(
    harness: Any,
) -> None:
    pipeline, repo, _, _, clock, _ = harness
    repo.add_entry(merged_into=uuid4())
    await pipeline.start("apply", {}, 1, proof(clock))
    report = await pipeline.start("apply", {}, 1, proof(clock))
    assert not report.pending_by_state and not report.pending_by_reason
    assert len(repo.events) == 1


@pytest.mark.parametrize("ending", ["refuted", "obsolete"])
async def test_lifecycle_propagation_skips_own_terminal_state(harness: Any, ending: str) -> None:
    pipeline, repo, _, _, clock, _ = harness
    child = repo.add_entry(
        project_key="child", **({"merged_into": uuid4()} if ending == "obsolete" else {})
    )
    await pipeline.start("apply", {"project_key": "child"}, 1, proof(clock))
    clock.at += timedelta(days=1)
    await pipeline.start("apply", {"project_key": "child"}, 1, proof(clock))
    before = len(repo.evaluations), len(repo.events)
    parent = repo.add_entry(merged_into=uuid4(), project_key="parent")
    repo.edges[("decisions", parent["id"])] = [("decisions", child["id"])]
    report = await pipeline.start("apply", {"project_key": "parent"}, 1, proof(clock))
    assert (len(repo.evaluations), len(repo.events)) == (before[0] + 1, before[1] + 1)
    invalidated = await pipeline.invalidate(report.run_id, "bad parent", proof(clock))
    assert invalidated.failure_reason is None
    assert repo.cache[("decisions", child["id"])]["state"] == ending
    assert len([e for e in repo.events if e["entity_id"] == child["id"]]) == before[1]


async def test_lifecycle_propagation_preflights_all_offenders(
    harness: Any, monkeypatch: Any
) -> None:
    from brain_v42.knowledge.decide import Decision
    from brain_v42.knowledge.ranks import State
    from brain_v42.services import knowledge_lifecycle_pipeline as module

    pipeline, repo, _, _, clock, _ = harness
    parent = repo.add_entry(merged_into=uuid4(), project_key="parent")
    children = sorted(
        [repo.add_entry(project_key="child") for _ in range(3)], key=lambda r: str(r["id"])
    )
    repo.edges[("decisions", parent["id"])] = [("decisions", r["id"]) for r in children]
    original = module.decide
    calls = 0

    def over_cap(observations: Any, day: Any) -> Any:
        nonlocal calls
        if observations and observations[0].source_kind == "propagation":
            calls += 1
            if calls > 1:
                return Decision(State.REFUTED, "bad", (), layer="propagation")
        return original(observations, day)

    monkeypatch.setattr(module, "decide", over_cap)
    report = await pipeline.start("apply", {"project_key": "parent"}, 1, proof(clock))
    assert report.offending_ids == [str(r["id"]) for r in children[1:]]
    assert len(repo.events) == len(repo.evaluations) == 1


async def test_lifecycle_invalidation_collects_offenders_and_compensates_others(
    harness: Any,
) -> None:
    from brain_v42.knowledge.decide import Decision
    from brain_v42.knowledge.ranks import State

    pipeline, repo, _, _, clock, _ = harness
    parent = repo.add_entry(merged_into=uuid4(), project_key="parent")
    children = sorted(
        [repo.add_entry(project_key="child") for _ in range(4)], key=lambda r: str(r["id"])
    )
    repo.edges[("decisions", parent["id"])] = [("decisions", r["id"]) for r in children]
    report = await pipeline.start("apply", {"project_key": "parent"}, 1, proof(clock))
    original = pipeline._propagation_decision
    offenders = {children[0]["id"], children[2]["id"]}

    async def over_cap(
        conn: Any, identity: Any, parents: Any, *, invalidating: bool = False
    ) -> Any:
        if identity[1] in offenders:
            return Decision(State.REFUTED, "bad", (), layer="propagation")
        return await original(conn, identity, parents, invalidating=invalidating)

    pipeline._propagation_decision = over_cap
    result = await pipeline.invalidate(report.run_id, "bad", proof(clock))
    assert result.status == "invalidated" and result.failure_reason == "propagation_cap_exceeded"
    assert result.offending_ids == sorted(str(eid) for eid in offenders)
    for child in children:
        assert repo.cache[("decisions", child["id"])]["state"] == (
            "contested" if child["id"] in offenders else "valid"
        )


async def test_lifecycle_resume_mid_propagation_does_not_duplicate_events(harness: Any) -> None:
    pipeline, repo, _, _, clock, _ = harness
    parent = repo.add_entry(merged_into=uuid4(), project_key="parent")
    children = [repo.add_entry(project_key="child") for _ in range(3)]
    repo.edges[("decisions", parent["id"])] = [("decisions", r["id"]) for r in children]
    original = pipeline._propagation_entry
    calls = 0

    async def crash(*args: Any, **kwargs: Any) -> None:
        nonlocal calls
        calls += 1
        if calls == 2:
            raise RuntimeError("interrupted sweep")
        await original(*args, **kwargs)

    pipeline._propagation_entry = crash
    with pytest.raises(RuntimeError, match="interrupted sweep"):
        await pipeline.start("apply", {"project_key": "parent"}, 1, proof(clock))
    rid = next(iter(repo.runs))
    pipeline._propagation_entry = original
    report = await pipeline.resume(rid, proof(clock))
    assert report.status == "applied" and len(repo.events) == 4
    assert len(repo.evaluations) == 4


@pytest.mark.parametrize("status", ["applied", "applying"])
async def test_lifecycle_recompute_ignores_deferred_unapplied_evaluation(
    harness: Any, status: str
) -> None:
    pipeline, repo, _, _, clock, _ = harness
    row = repo.add_entry()
    await pipeline.start("apply", {}, 1, proof(clock))
    clock.at += timedelta(days=1)
    repo.defer = StaleAtApply("stale_at_apply")
    deferred = await pipeline.start("apply", {}, 1, proof(clock))
    repo.defer = None
    # A stale evaluation in an unfinished run was never an applied baseline.
    repo.runs[deferred.run_id]["status"] = status
    decision = await pipeline._recomputed(None, ("decisions", row["id"]))
    assert decision.state == "valid"


async def test_lifecycle_invalidation_defers_edited_entry(harness: Any) -> None:
    pipeline, repo, _, _, clock, _ = harness
    row = repo.add_entry(merged_into=uuid4())
    first = await pipeline.start("apply", {}, 1, proof(clock))
    second = await pipeline.start("apply", {}, 1, proof(clock))
    row["text_fields"]["description"] = "edited"
    row["content_updated_at"] = clock.at + timedelta(seconds=1)
    result = await pipeline.invalidate(second.run_id, "bad", proof(clock))
    assert result.deferred_by_cause == {"stale_at_apply": 1}
    assert len(repo.events) == 1
    assert repo.runs[first.run_id]["status"] == "applied"


async def test_lifecycle_backup_is_not_rechecked_per_chunk(harness: Any) -> None:
    pipeline, repo, _, _, clock, _ = harness
    for _ in range(201):
        repo.add_entry(merged_into=uuid4())
    supplied = proof(clock)
    repo.after_apply = lambda: setattr(clock, "at", NOW + timedelta(hours=2))
    result = await pipeline.start("apply", {}, 100, supplied)
    assert result.status == "applied" and len(repo.events) == 201
    assert result.backup_run_id == supplied.run_id
    assert result.backup_sealed_at == supplied.sealed_at.isoformat()


@pytest.mark.parametrize("status", ["manifest_frozen", "evaluated"])
async def test_lifecycle_resume_rejects_tampered_manifest_before_count(
    harness: Any, status: str
) -> None:
    pipeline, repo, client, _, _, _ = harness
    repo.add_entry()
    await repo.create_run(None, "tampered", "dry_run", {})
    await repo.record_run(None, "tampered", counts={"ceiling_usd": 1})
    await pipeline._freeze(await pipeline._read_run("tampered"))
    if status == "evaluated":
        await pipeline._submit(await pipeline._read_run("tampered"), None)
        await pipeline._collect(await pipeline._read_run("tampered"))
        await pipeline._evaluate(await pipeline._read_run("tampered"))
    submitted = client.submissions
    client.manifest = None
    repo.runs["tampered"]["manifest_digest"] = "wrong"
    result = await pipeline.resume("tampered")
    assert result.status == "failed" and result.failure_reason == "manifest_digest_mismatch"
    assert client.manifest is None and client.submissions == submitted


async def test_lifecycle_reports_compensation_separately(harness: Any) -> None:
    pipeline, repo, _, _, clock, _ = harness
    repo.add_entry(merged_into=uuid4())
    run = await pipeline.start("apply", {}, 1, proof(clock))
    result = await pipeline.invalidate(run.run_id, "bad", proof(clock))
    assert result.applied_by_state == {"obsolete": 1}
    assert result.compensated_by_state == {"valid": 1}


async def test_lifecycle_report_counts_compensation_outside_original_scope(harness: Any) -> None:
    pipeline, repo, _, _, clock, _ = harness
    parent = repo.add_entry(project_key="parent")
    child = repo.add_entry(project_key="child")
    repo.edges[("decisions", parent["id"])] = [("decisions", child["id"])]
    scope = {"project_key": "parent"}
    first = await pipeline.start("dry_run", scope, 1)
    clock.at += timedelta(days=1)
    await pipeline.start("apply", scope, 1, proof(clock))
    result = await pipeline.invalidate(first.run_id, "bad confirmation", proof(clock))
    assert result.compensated_by_state == {"valid": 2}
    assert repo.cache[("decisions", child["id"])]["state"] == "valid"


async def test_lifecycle_incomplete_evaluation_is_not_pending(harness: Any) -> None:
    pipeline, repo, client, _, _, _ = harness
    repo.add_entry()
    client.status = extraction.ResultStatus.ERRORED
    result = await pipeline.start("dry_run", {}, 1)
    assert not result.pending_by_state and not result.pending_by_reason


async def test_lifecycle_same_state_new_reason_is_an_event(harness: Any) -> None:
    pipeline, repo, _, _, clock, _ = harness
    row = repo.add_entry(merged_into=uuid4())
    await pipeline.start("apply", {}, 1, proof(clock))
    row.update(merged_into=None, status="superseded", successor_exists=True, superseded_by=uuid4())
    clock.at += timedelta(days=1)
    await pipeline.start("apply", {}, 1, proof(clock))
    assert len(repo.events) == 2
    assert repo.events[-1]["from_state"] == repo.events[-1]["to_state"] == "obsolete"
    assert repo.events[0]["reason"] != repo.events[-1]["reason"]


async def test_lifecycle_first_confirmed_valid_records_check(harness: Any) -> None:
    pipeline, repo, _, sources, clock, _ = harness
    repo.add_entry()
    sources.failed = frozenset()
    original = sources.snapshot

    async def valid(day: Any) -> Any:
        snapshot = await original(day)
        snapshot.sources.facts["ci.system"] = "gitlab-ci"
        return snapshot

    sources.snapshot = valid
    await pipeline.start("apply", {}, 1, proof(clock))
    clock.at += timedelta(days=1)
    await pipeline.start("apply", {}, 1, proof(clock))
    assert len(repo.events) == 1
    assert repo.events[0]["from_state"] is None and repo.events[0]["to_state"] == "valid"


async def test_lifecycle_success_clears_previous_deferral(harness: Any) -> None:
    pipeline, repo, _, _, clock, _ = harness
    row = repo.add_entry(merged_into=uuid4())
    clock.at = NOW.replace(hour=0, minute=45)
    result = await pipeline.start("apply", {}, 1, proof(clock))
    await pipeline._defer(result.run_id, "decisions", str(row["id"]), "lock_timeout")
    clock.at = NOW.replace(hour=4)
    result = await pipeline.resume(result.run_id, proof(clock))
    assert result.deferred_by_cause == {} and len(repo.events) == 1
