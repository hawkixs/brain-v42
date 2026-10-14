"""Drive lifecycle checkpoints while all external measurements remain injected.

Services may import both repositories and pure knowledge rules without creating
a module cycle. Only the repository touches lifecycle tables. Each operational
checkpoint commits before its side effect; each event owns one transaction.

066 permits one evaluation per run/entry. A propagation sweep therefore owns a
linked, structural-only apply run, recorded in the parent run's metadata. It
never re-decides a dependent's checks or recursively propagates its own events.
Invalidation also invalidates this evidence run and compensates its dependents.
"""

from __future__ import annotations

from collections import Counter
from collections.abc import Awaitable, Callable, Mapping, Sequence
from contextlib import AbstractAsyncContextManager
from dataclasses import asdict, replace
from datetime import date
from decimal import Decimal
from typing import Any
from uuid import UUID, uuid4

import structlog
from sqlalchemy.ext.asyncio import AsyncConnection, AsyncEngine

from brain_v42.knowledge import extraction, facts_registry
from brain_v42.knowledge.checks import check
from brain_v42.knowledge.decide import RULES_VERSION, Decision, Observation, decide, fingerprint
from brain_v42.knowledge.hysteresis import EvaluationRecord, confirmed
from brain_v42.knowledge.ranks import (
    RANKS_VERSION,
    Centrality,
    Polarity,
    State,
    Temporal,
    ValidFromPrecision,
)
from brain_v42.knowledge.structural import structural_observations
from brain_v42.repositories.pg_knowledge_lifecycle import (
    ApplyDeferred,
    ApplyWindowClosed,
    LockedApplyInputs,
    PgKnowledgeLifecycleRepo,
    RunStatus,
    StaleAtApply,
    check_apply_window,
    whole_run_lock,
)
from brain_v42.services.knowledge_lifecycle_evidence import (
    BackupProof as BackupProof,
)
from brain_v42.services.knowledge_lifecycle_evidence import (
    Clock,
    ExtractionClient,
    Gates,
    Mode,
    SourcesProvider,
    chunk_window_open,
    entry_text,
    pack_manifest,
    pack_observation,
    record,
    structural_inputs,
    structural_signature,
    unpack_manifest,
    unpack_observation,
    utc,
)
from brain_v42.services.knowledge_lifecycle_evidence import (
    EvaluationSources as EvaluationSources,
)
from brain_v42.services.knowledge_lifecycle_evidence import (
    PipelineRefusal as PipelineRefusal,
)
from brain_v42.services.knowledge_lifecycle_evidence import (
    RunReport as RunReport,
)

log = structlog.get_logger(__name__)
CHUNK_SIZE = 200
BACKUP_DECLARATION = "backup proof declared by the coordination, not re-read"
TERMINAL = {RunStatus.APPLIED, RunStatus.FAILED, RunStatus.INVALIDATED}
ENDING = {State.OBSOLETE, State.REFUTED}
RunLock = Callable[[AsyncEngine], AbstractAsyncContextManager[bool]]
CommitGate = Callable[[AsyncConnection], Awaitable[None]]
Identity = tuple[str, UUID]


class LifecyclePipeline:
    """Keep transaction ownership here so a port cannot silently checkpoint work."""

    def __init__(
        self,
        engine: AsyncEngine,
        client: ExtractionClient,
        sources: SourcesProvider,
        clock: Clock,
        gates: Gates,
        *,
        repo: PgKnowledgeLifecycleRepo | None = None,
        run_lock: RunLock = whole_run_lock,
        commit_gate: CommitGate = check_apply_window,
    ) -> None:
        self.engine, self.client, self.sources = engine, client, sources
        self.clock, self.gates = clock, gates
        self.repo = repo if repo is not None else PgKnowledgeLifecycleRepo()
        self.run_lock, self.commit_gate = run_lock, commit_gate

    async def _gate(self, mode: str) -> None:
        refusal = await self.gates.check(mode)
        if refusal:
            raise PipelineRefusal(refusal)

    def _backup_gate(self, proof: BackupProof | None) -> None:
        if proof is None or not proof.run_id.strip():
            raise PipelineRefusal("backup_proof_missing")
        age = (utc(self.clock.now()) - utc(proof.sealed_at)).total_seconds()
        if not 0 <= age <= 3600:
            raise PipelineRefusal("backup_proof_expired")

    async def _check_backup(self, run_id: str, proof: BackupProof | None, *, phase: str) -> None:
        self._backup_gate(proof)
        assert proof is not None
        async with self.engine.begin() as conn:
            if phase == "backup_apply":
                run = await self.repo.read_run(conn, run_id)
                if phase in run["counts"]:
                    return
            await self.repo.record_run(
                conn,
                run_id,
                counts={
                    phase: {
                        "run_id": proof.run_id,
                        "sealed_at": utc(proof.sealed_at).isoformat(),
                    }
                },
            )

    async def start(
        self,
        mode: str,
        scope: Mapping[str, Any],
        ceiling_usd: float,
        backup_proof: BackupProof | None = None,
        *,
        run_id: str | None = None,
    ) -> RunReport:
        mode = Mode(mode)
        if set(scope) - {"project_key", "project_keys"}:
            raise ValueError("Scope accepts project_key or project_keys")
        if "project_key" in scope and "project_keys" in scope:
            raise ValueError("Scope must choose one project filter")
        async with self.run_lock(self.engine) as acquired:
            if not acquired:
                raise PipelineRefusal("run_locked")
            await self._gate(mode)
            if run_id is None:
                run_id = "lifecycle-" + uuid4().hex
            async with self.engine.begin() as conn:
                await self.repo.create_run(conn, run_id, mode, scope)
                await self.repo.record_run(conn, run_id, counts={"ceiling_usd": ceiling_usd})
            return await self._drive(run_id, backup_proof)

    async def resume(
        self,
        run_id: str,
        backup_proof: BackupProof | None = None,
    ) -> RunReport:
        async with self.run_lock(self.engine) as acquired:
            if not acquired:
                raise PipelineRefusal("run_locked")
            run = await self._read_run(run_id)
            await self._gate(run["mode"])
            if run["mode"] == Mode.APPLY and run["status"] not in TERMINAL:
                self._backup_gate(backup_proof)
            if run["status"] not in TERMINAL and "manifest" in run["counts"]:
                manifest = unpack_manifest(run_id, run["counts"]["manifest"])
                if extraction.manifest_digest(manifest) != run["manifest_digest"]:
                    return await self._fail(run, "manifest_digest_mismatch")
            return await self._drive(run_id, backup_proof)

    async def _read_run(self, run_id: str) -> dict[str, Any]:
        async with self.engine.begin() as conn:
            return await self.repo.read_run(conn, run_id)

    async def _edge(self, run_id: str, source: str, target: str, **kwargs: Any) -> None:
        async with self.engine.begin() as conn:
            await self.repo.transition_run(conn, run_id, source, target, **kwargs)

    async def _fail(self, run: Mapping[str, Any], reason: str) -> RunReport:
        await self._edge(run["run_id"], run["status"], RunStatus.FAILED, reason=reason)
        return await self.report(run["run_id"])

    async def _drive(self, run_id: str, proof: BackupProof | None) -> RunReport:
        while True:
            run = await self._read_run(run_id)
            status = RunStatus(run["status"])
            if status in TERMINAL:
                return await self.report(run_id)
            if status == RunStatus.CREATED:
                await self._freeze(run)
            elif status == RunStatus.MANIFEST_FROZEN:
                if not await self._submit(run, proof):
                    return await self.report(run_id)
            elif status == RunStatus.SUBMITTING:
                if not run["batch_ids"]:
                    return await self._fail(run, "ambiguous_submission")
                await self._edge(run_id, status, RunStatus.SUBMITTED)
            elif status == RunStatus.SUBMITTED:
                await self._collect(run)
            elif status == RunStatus.RESULTS_STORED:
                await self._evaluate(run)
            elif status == RunStatus.EVALUATED:
                if run["mode"] == Mode.APPLY:
                    await self._gate(run["mode"])
                    await self._check_backup(run_id, proof, phase="backup_apply")
                await self._edge(run_id, status, RunStatus.APPLYING)
            elif status == RunStatus.APPLYING:
                if run["mode"] == Mode.APPLY:
                    if not await self._apply(run, proof):
                        return await self.report(run_id)
                    if not await self._propagate(await self._read_run(run_id), proof):
                        return await self.report(run_id)
                await self._edge(run_id, status, RunStatus.APPLIED)

    async def _scope_entries(self, conn: AsyncConnection, scope: Mapping[str, Any]) -> list[Any]:
        rows = await self.repo.list_entries(conn, scope.get("project_key"))
        if "project_keys" in scope:
            rows = [r for r in rows if r["project_key"] in scope["project_keys"]]
        return sorted(
            rows,
            key=lambda r: (
                r["project_key"] != "brain-v42",
                r["project_key"] or "",
                r["entity_type"],
                str(r["id"]),
            ),
        )

    async def _extraction(self, conn: AsyncConnection, row: Mapping[str, Any]) -> Any:
        text = entry_text(row)
        if not extraction.screen(text.text):
            return None
        return await self.repo.read_extraction(
            conn,
            row["entity_type"],
            row["id"],
            text.digest,
            model=extraction.MODEL,
            prompt_version=extraction.PROMPT_VERSION,
        )

    async def _freeze(self, run: Mapping[str, Any]) -> None:
        run_id = run["run_id"]
        async with self.engine.begin() as conn:
            rows = await self._scope_entries(conn, run["scope"])
            needed = [entry_text(r) for r in rows if await self._extraction(conn, r) is None]
            # build_requests sorts within a project; re-number to retain global attribution.
            requests: list[extraction.BatchRequest] = []
            screened: list[extraction.EntryRevision] = []
            for project in dict.fromkeys(r["project_key"] for r in rows):
                ids = {str(r["id"]) for r in rows if r["project_key"] == project}
                part = extraction.build_requests(
                    run_id, [e for e in needed if e.entity_id in ids], facts_registry
                )
                screened.extend(part.screened)
                for request in part.requests:
                    requests.append(replace(request, custom_id=f"{run_id}-{len(requests):05d}"))
            manifest = extraction.Manifest(run_id, tuple(requests), tuple(screened))
            payload = pack_manifest(manifest)
            await self.repo.insert_requests(
                conn, run_id, {r["custom_id"]: r for r in payload["requests"]}
            )
            await self.repo.record_run(
                conn,
                run_id,
                counts={
                    "manifest": payload,
                    "entries": [[r["entity_type"], str(r["id"])] for r in rows],
                    "request_statuses": {r.custom_id: "pending" for r in requests},
                },
            )
            await self.repo.transition_run(
                conn,
                run_id,
                RunStatus.CREATED,
                RunStatus.MANIFEST_FROZEN,
                manifest_digest=extraction.manifest_digest(manifest),
            )

    async def _submit(self, run: Mapping[str, Any], proof: BackupProof | None) -> bool:
        manifest = unpack_manifest(run["run_id"], run["counts"]["manifest"])
        if extraction.manifest_digest(manifest) != run["manifest_digest"]:
            await self._fail(run, "manifest_digest_mismatch")
            return False
        await self._gate(run["mode"])
        counts = await self.client.count(manifest)
        await self._gate(run["mode"])
        async with self.engine.begin() as conn:
            await self.repo.record_run(
                conn,
                run["run_id"],
                counts={
                    "token_counts": dict(counts.input_tokens),
                    "worst_case_usd": counts.worst_case_usd,
                },
            )
        try:
            extraction.admit(manifest, counts, run["counts"]["ceiling_usd"])
        except ValueError:
            await self._fail(run, "cost_guard_refused")
            return False
        async with self.engine.begin() as conn:
            await self.repo.transition_run(
                conn, run["run_id"], RunStatus.MANIFEST_FROZEN, RunStatus.SUBMITTING
            )
            if not manifest.requests:
                # Both edges share one commit: no API call can become ambiguous.
                await self.repo.transition_run(
                    conn, run["run_id"], RunStatus.SUBMITTING, RunStatus.SUBMITTED
                )
                return True
        if manifest.requests:
            # A lost response deliberately leaves submitting without an id.
            batch_id = await self.client.submit(manifest)
            async with self.engine.begin() as conn:
                await self.repo.record_run(conn, run["run_id"], batch_id=batch_id)
                await self.repo.transition_run(
                    conn, run["run_id"], RunStatus.SUBMITTING, RunStatus.SUBMITTED
                )
        return True

    async def discard_failed_batch(
        self,
        run_id: str,
        batch_id: str,
        backup_proof: BackupProof | None = None,
    ) -> RunReport:
        """Account for a discovered ambiguous batch without admitting its evidence."""
        async with self.run_lock(self.engine) as acquired:
            if not acquired:
                raise PipelineRefusal("run_locked")
            run = await self._read_run(run_id)
            await self._gate(run["mode"])
            if (
                run["status"] != RunStatus.FAILED
                or run["counts"].get("failure_reason") != "ambiguous_submission"
            ):
                raise PipelineRefusal("discard_requires_ambiguous_submission")
            if batch_id in run["counts"].get("discarded_batches", []):
                return await self.report(run_id)
            manifest = unpack_manifest(run_id, run["counts"]["manifest"])
            async with self.engine.begin() as conn:
                await self.repo.record_run(conn, run_id, batch_id=batch_id)
            results = await self.client.collect(batch_id, manifest)
            await self._gate(run["mode"])
            async with self.engine.begin() as conn:
                current = await self.repo.read_run(conn, run_id)
                usage = dict(current["usage"])
                for cid, result in results.items():
                    await self.repo.checkpoint_request(conn, run_id, cid, result.status)
                    await self.repo.set_count_key(
                        conn, run_id, "request_statuses", cid, result.status.value
                    )
                    for key, value in asdict(result.usage).items():
                        usage[key] = usage.get(key, 0) + value
                await self.repo.record_run(
                    conn,
                    run_id,
                    usage=usage,
                    real_cost=(current["real_cost"] or Decimal(0))
                    + Decimal(str(extraction.real_cost_usd(results))),
                    counts={
                        "discarded_batches": [
                            *current["counts"].get("discarded_batches", []),
                            batch_id,
                        ],
                    },
                )
            return await self.report(run_id)

    async def _collect(self, run: Mapping[str, Any]) -> None:
        manifest = unpack_manifest(run["run_id"], run["counts"]["manifest"])
        async with self.engine.begin() as conn:
            pending = await self.repo.pending_requests(conn, run["run_id"])
        if pending:
            results = await self.client.collect(run["batch_ids"][0], manifest)
            usage = dict(run["usage"])
            real_cost = run["real_cost"] or Decimal(0)
            for request in pending:
                cid = request["custom_id"]
                result = results[cid]
                async with self.engine.begin() as conn:
                    if not await self.repo.checkpoint_request(
                        conn, run["run_id"], cid, result.status
                    ):
                        continue
                    for item in result.extractions:
                        await self.repo.insert_extraction(
                            conn,
                            run_id=run["run_id"],
                            entity_type=item.entity_type,
                            entity_id=UUID(item.entity_id),
                            entry_digest=item.digest,
                            model=extraction.MODEL,
                            prompt_version=extraction.PROMPT_VERSION,
                            assertions=item.assertions,
                        )
                    await self.repo.set_count_key(
                        conn, run["run_id"], "request_statuses", cid, result.status.value
                    )
                    usage = {k: usage.get(k, 0) + v for k, v in asdict(result.usage).items()}
                    cost = Decimal(str(extraction.real_cost_usd({cid: result})))
                    real_cost += cost
                    await self.repo.record_run(
                        conn,
                        run["run_id"],
                        usage=usage,
                        real_cost=real_cost,
                    )
        await self._edge(run["run_id"], RunStatus.SUBMITTED, RunStatus.RESULTS_STORED)

    async def _evaluate(self, run: Mapping[str, Any]) -> None:
        day = utc(self.clock.now()).date()
        sources = await self.sources.snapshot(day)
        async with self.engine.begin() as conn:
            await self.repo.record_run(
                conn, run["run_id"], counts={"origin_main_sha": sources.sources.origin_main_sha}
            )
            rows = {(r["entity_type"], str(r["id"])): r for r in await self.repo.list_entries(conn)}
        for kind, eid in run["counts"]["entries"]:
            row = rows.get((kind, eid))
            if row is None:
                await self._defer(run["run_id"], kind, eid, "entry_missing")
                continue
            async with self.engine.begin() as conn:
                previous = await self.repo.evaluations_for_entry(conn, kind, UUID(eid))
                if any(e["run_id"] == run["run_id"] for e in previous):
                    continue
                stored = await self._extraction(conn, row)
                observations = structural_observations(kind, row)
                complete = stored is not None
                structural_candidate = decide(observations, day)
                if stored is not None and structural_candidate.layer != "structural":
                    for assertion in stored["assertions"]:
                        ref = (
                            assertion["fact_key"]
                            if assertion["check"] == "fact"
                            else assertion["ref"]
                        )
                        if (
                            assertion["check"] in sources.failed
                            or f"{assertion['check']}:{ref}" in sources.failed
                        ):
                            complete = False
                            continue
                        observation = check(
                            assertion, sources.sources, entry_text(row).text, entry_id=eid
                        )
                        if observation is not None:
                            observations.append(observation)
                        elif self._unreachable(assertion, sources):
                            complete = False
                decision = decide(observations, day)
                if decision.layer == "structural":
                    complete = True  # Rules 1-2 require no external check.
                cache = await self.repo.read_cache(conn, kind, UUID(eid))
                evaluation = await self._insert_evaluation(
                    conn,
                    run["run_id"],
                    row,
                    day,
                    sources.sources.origin_main_sha,
                    observations,
                    decision,
                    complete,
                    cache,
                    None if stored is None else stored["id"],
                )
                await self._save_snapshot(conn, run["run_id"], evaluation["id"], row)
        await self._edge(run["run_id"], RunStatus.RESULTS_STORED, RunStatus.EVALUATED)

    @staticmethod
    def _unreachable(assertion: Mapping[str, Any], sources: EvaluationSources) -> bool:
        kind, ref = assertion["check"], assertion["ref"]
        if kind == "fact":
            return (
                assertion["fact_key"] in sources.sources.registry
                and sources.sources.facts.get(assertion["fact_key"]) is None
            )
        if kind == "pr" and str(ref).lstrip("#").isdigit():
            return int(str(ref).lstrip("#")) not in sources.sources.prs
        if kind == "ticket":
            return ref.lower() not in sources.sources.tickets
        return False

    async def _insert_evaluation(
        self,
        conn: AsyncConnection,
        run_id: str,
        row: Mapping[str, Any],
        day: date,
        sha: str,
        observations: Sequence[Observation],
        decision: Decision,
        complete: bool,
        cache: Mapping[str, Any] | None,
        extraction_id: int | None = None,
    ) -> dict[str, Any]:
        return await self.repo.insert_evaluation(
            conn,
            {
                "run_id": run_id,
                "entity_type": row["entity_type"],
                "entity_id": row["id"],
                "entry_digest": entry_text(row).digest,
                "extraction_id": extraction_id,
                "expected_cache_revision": 0 if cache is None else cache["revision"],
                "rules_version": RULES_VERSION,
                "ranks_version": RANKS_VERSION,
                "registry_version": facts_registry.REGISTRY_VERSION,
                "origin_main_sha": sha,
                "observed_day": day,
                "complete": complete,
                "state": decision.state.value,
                "reason": decision.reason,
                "fingerprint": fingerprint(decision, observations),
            },
            [pack_observation(o) for o in observations],
        )

    async def _save_snapshot(
        self,
        conn: AsyncConnection,
        run_id: str,
        evaluation_id: int,
        row: Mapping[str, Any],
    ) -> None:
        snapshot = {
            "structure": structural_signature(structural_observations(row["entity_type"], row)),
            "structural_inputs": structural_inputs(row),
            "content_updated_at": utc(row["content_updated_at"]).isoformat(),
        }
        await self.repo.set_count_key(conn, run_id, "snapshots", str(evaluation_id), snapshot)

    async def _evidence(
        self,
        conn: AsyncConnection,
        evaluation: Mapping[str, Any],
    ) -> tuple[Decision, list[Observation]]:
        observations = [
            unpack_observation(o)
            for o in await self.repo.read_justifications(conn, evaluation["id"])
        ]
        computed = decide(observations, evaluation["observed_day"])
        # A propagation lift restores an applied baseline rather than deciding old checks.
        decision = replace(computed, state=State(evaluation["state"]), reason=evaluation["reason"])
        return decision, observations

    async def _run_evaluations(self, run: Mapping[str, Any]) -> list[dict[str, Any]]:
        result: list[dict[str, Any]] = []
        async with self.engine.begin() as conn:
            for kind, eid in await self.repo.entries_for_run(conn, run["run_id"]):
                result.extend(
                    e
                    for e in await self.repo.evaluations_for_entry(conn, kind, eid)
                    if e["run_id"] == run["run_id"]
                )
        return sorted(result, key=lambda e: e["id"])

    async def _history(
        self,
        conn: AsyncConnection,
        kind: str,
        eid: UUID,
    ) -> list[tuple[dict[str, Any], Decision, EvaluationRecord]]:
        result = []
        for row in await self.repo.evaluations_for_entry(conn, kind, eid):
            decision, _ = await self._evidence(conn, row)
            result.append((row, decision, record(row, decision)))
        return result

    async def _apply(self, run: Mapping[str, Any], proof: BackupProof | None) -> bool:
        evaluations = await self._run_evaluations(run)
        for offset in range(0, len(evaluations), CHUNK_SIZE):
            await self._gate(run["mode"])
            if not chunk_window_open(self.clock.now()):
                return False
            for evaluation in evaluations[offset : offset + CHUNK_SIZE]:
                try:
                    async with self.engine.begin() as conn:
                        kind, eid = evaluation["entity_type"], evaluation["entity_id"]
                        events = await self.repo.events_for_entry(conn, kind, eid)
                        if any(
                            e["evaluation_id"] == evaluation["id"] and e["kind"] == "transition"
                            for e in events
                        ):
                            await self.repo.set_count_key(
                                conn, run["run_id"], "deferred", f"{kind}:{eid}", None
                            )
                            continue
                        decision, _ = await self._evidence(conn, evaluation)
                        history = await self._history(conn, kind, eid)
                        prior = [item[2] for item in history if item[0]["id"] < evaluation["id"]]
                        if not evaluation["complete"] or not confirmed(
                            prior, record(evaluation, decision)
                        ):
                            continue
                        inputs = await self.repo.lock_apply_inputs(conn, kind, eid)
                        snapshot = run["counts"].get("snapshots", {}).get(str(evaluation["id"]))
                        if (
                            snapshot is None
                            or entry_text(inputs.entry).digest != evaluation["entry_digest"]
                            or structural_signature(structural_observations(kind, inputs.entry))
                            != snapshot["structure"]
                            or structural_inputs(inputs.entry) != snapshot["structural_inputs"]
                            or utc(inputs.entry["content_updated_at"]).isoformat()
                            != snapshot["content_updated_at"]
                            or inputs.revision != evaluation["expected_cache_revision"]
                        ):
                            raise StaleAtApply("stale_at_apply")
                        if (
                            inputs.cache is not None
                            and inputs.cache["state"] == decision.state
                            and inputs.cache["reason"] == decision.reason
                        ):
                            await self.repo.set_count_key(
                                conn, run["run_id"], "deferred", f"{kind}:{eid}", None
                            )
                            continue
                        # None -> valid records the first confirmed check for readers.
                        # A changed reason is visible evidence even at the same state.
                        await self.repo.append_transition(
                            conn, evaluation["id"], inputs, **self._event_values(decision)
                        )
                        await self.repo.set_count_key(
                            conn, run["run_id"], "deferred", f"{kind}:{eid}", None
                        )
                        await self.commit_gate(conn)
                except ApplyWindowClosed:
                    return False
                except (StaleAtApply, ApplyDeferred) as exc:
                    cause = "stale_at_apply" if isinstance(exc, StaleAtApply) else str(exc)
                    await self._defer(
                        run["run_id"],
                        evaluation["entity_type"],
                        str(evaluation["entity_id"]),
                        cause,
                    )
        return True

    @staticmethod
    def _event_values(decision: Decision) -> dict[str, Any]:
        return {
            "to_state": decision.state.value,
            "reason": decision.reason,
            "valid_from": decision.end_date,
            "valid_from_precision": (
                decision.end_date_precision or ValidFromPrecision.UNKNOWN
            ).value,
        }

    async def _defer(self, run_id: str, kind: str, eid: str, cause: str) -> None:
        async with self.engine.begin() as conn:
            await self.repo.set_count_key(conn, run_id, "deferred", f"{kind}:{eid}", cause)
        log.info("lifecycle_entry_deferred", run_id=run_id, entity_id=eid, cause=cause)

    async def _changed_parents(self, run: Mapping[str, Any]) -> list[Identity]:
        result = []
        async with self.engine.begin() as conn:
            for kind, eid in await self.repo.entries_for_run(conn, run["run_id"]):
                for event in await self.repo.events_for_entry(conn, kind, eid):
                    evaluation = await self.repo.read_evaluation(conn, event["evaluation_id"])
                    belongs = (
                        event.get("invalidated_run_id") == run["run_id"]
                        or evaluation["run_id"] == run["run_id"]
                    )
                    if (
                        belongs
                        and event["from_state"] != event["to_state"]
                        and (event["from_state"] in ENDING or event["to_state"] in ENDING)
                    ):
                        result.append((kind, eid))
        return sorted(set(result), key=lambda pair: (pair[0], str(pair[1])))

    async def _propagation_plan(self, run: Mapping[str, Any]) -> dict[Identity, list[Identity]]:
        """Derive markers from current applied parents, including other active parents."""
        targets: dict[Identity, list[Identity]] = {}
        changed = await self._changed_parents(run)
        async with self.engine.begin() as conn:
            for kind, eid in changed:
                for dependent in await self.repo.direct_dependents(conn, kind, eid):
                    targets.setdefault(dependent, []).append((kind, eid))
            # An invalidated propagation run also owns dependents whose marker must be lifted.
            child = run["counts"].get("propagation_run_id")
            if run["status"] == RunStatus.INVALIDATED and child:
                for dependent in await self.repo.entries_for_run(conn, child):
                    targets.setdefault(dependent, [])
            active: dict[Identity, list[Identity]] = {key: [] for key in targets}
            for row in await self.repo.list_entries(conn):
                parent = row["entity_type"], row["id"]
                cache = await self.repo.read_cache(conn, *parent)
                if cache is None or cache["state"] not in ENDING:
                    continue
                for dependent in await self.repo.direct_dependents(conn, *parent):
                    if dependent in active:
                        active[dependent].append(parent)
            return active

    async def _baseline(self, conn: AsyncConnection, identity: Identity) -> Decision:
        """Restore applied non-propagation evidence, never an unconfirmed candidate."""
        evaluations = {e["id"]: e for e in await self.repo.evaluations_for_entry(conn, *identity)}
        for event in reversed(await self.repo.events_for_entry(conn, *identity)):
            evaluation = evaluations[event["evaluation_id"]]
            if event["kind"] == "transition" and evaluation["invalidated"]:
                continue
            decision, _ = await self._evidence(conn, evaluation)
            if decision.layer != "propagation":
                return Decision(
                    State(event["to_state"]),
                    event["reason"],
                    (),
                    event.get("valid_from"),
                    ValidFromPrecision(event["valid_from_precision"]),
                )
        return Decision(State.VALID, "declaration_only", ())

    async def _propagate(
        self,
        run: Mapping[str, Any],
        proof: BackupProof | None,
    ) -> bool:
        if run["counts"].get("propagation_done"):
            return True
        plan = await self._propagation_plan(run)
        child_id = run["counts"].get("propagation_run_id")
        if not plan and child_id is None:
            async with self.engine.begin() as conn:
                await self.repo.record_run(conn, run["run_id"], counts={"propagation_done": True})
            return True
        if child_id is None:
            child_id = "propagation-" + uuid4().hex
            async with self.engine.begin() as conn:
                await self.repo.create_run(
                    conn, child_id, Mode.APPLY, {"propagation_for": run["run_id"]}
                )
                await self.repo.record_run(
                    conn, run["run_id"], counts={"propagation_run_id": child_id}
                )
        child = await self._read_run(child_id)
        if child["status"] == RunStatus.FAILED:
            async with self.engine.begin() as conn:
                await self.repo.record_run(
                    conn,
                    run["run_id"],
                    counts={
                        "offending_ids": child["counts"].get("offending_ids", []),
                    },
                )
            await self._fail(run, child["counts"]["failure_reason"])
            return False
        if child["status"] == RunStatus.CREATED:
            await self._edge(
                child_id,
                RunStatus.CREATED,
                RunStatus.MANIFEST_FROZEN,
                manifest_digest=extraction.manifest_digest(extraction.Manifest(child_id, ())),
            )
        chain = [
            RunStatus.MANIFEST_FROZEN,
            RunStatus.SUBMITTING,
            RunStatus.SUBMITTED,
            RunStatus.RESULTS_STORED,
            RunStatus.EVALUATED,
            RunStatus.APPLYING,
        ]
        child = await self._read_run(child_id)
        while child["status"] in chain[:-1]:
            position = chain.index(RunStatus(child["status"]))
            await self._edge(child_id, chain[position], chain[position + 1])
            child = await self._read_run(child_id)
        if child["status"] == RunStatus.APPLIED:
            async with self.engine.begin() as conn:
                await self.repo.record_run(conn, run["run_id"], counts={"propagation_done": True})
            return True
        return await self._propagation_entries(run, child_id, plan, proof)

    async def _propagation_entries(
        self,
        run: Mapping[str, Any],
        child_id: str,
        plan: Mapping[Identity, list[Identity]],
        proof: BackupProof | None,
    ) -> bool:
        prepared, offenders = await self._prepare_propagation(run, plan)
        if offenders:
            # Persist the child failure first; resume can finish failing the parent.
            async with self.engine.begin() as conn:
                for rid in (child_id, run["run_id"]):
                    await self.repo.record_run(conn, rid, counts={"offending_ids": offenders})
                await self.repo.transition_run(
                    conn,
                    child_id,
                    RunStatus.APPLYING,
                    RunStatus.FAILED,
                    reason="propagation_cap_exceeded",
                )
            await self._fail(run, "propagation_cap_exceeded")
            return False
        identities = list(prepared)
        for offset in range(0, len(identities), CHUNK_SIZE):
            await self._gate(Mode.APPLY)
            if not chunk_window_open(self.clock.now()):
                return False
            for identity in identities[offset : offset + CHUNK_SIZE]:
                try:
                    decision, inputs = prepared[identity]
                    await self._propagation_entry(
                        run, child_id, identity, plan[identity], decision, inputs
                    )
                except ApplyWindowClosed:
                    return False
                except (StaleAtApply, ApplyDeferred) as exc:
                    await self._defer(
                        run["run_id"],
                        identity[0],
                        str(identity[1]),
                        "stale_at_apply" if isinstance(exc, StaleAtApply) else str(exc),
                    )
        await self._edge(child_id, RunStatus.APPLYING, RunStatus.APPLIED)
        async with self.engine.begin() as conn:
            await self.repo.record_run(conn, run["run_id"], counts={"propagation_done": True})
        return True

    async def _propagation_entry(
        self,
        run: Mapping[str, Any],
        child_id: str,
        identity: Identity,
        parents: Sequence[Identity],
        decision: Decision,
        expected: LockedApplyInputs,
    ) -> None:
        async with self.engine.begin() as conn:
            inputs = await self.repo.lock_apply_inputs(conn, *identity)
            baseline = await self._baseline(conn, identity)
            if baseline.state in ENDING:
                return
            if inputs.entry != expected.entry or inputs.revision != expected.revision:
                raise StaleAtApply("stale_at_apply")
            day = utc(self.clock.now()).date()
            observations = self._propagation_observations(identity, parents)
            history = await self.repo.evaluations_for_entry(conn, *identity)
            previous = next((e for e in history if e["run_id"] == child_id), None)
            if previous is not None:
                events = await self.repo.events_for_entry(conn, *identity)
                if any(e["evaluation_id"] == previous["id"] for e in events):
                    await self.repo.set_count_key(
                        conn, run["run_id"], "deferred", f"{identity[0]}:{identity[1]}", None
                    )
                    return
                if (
                    previous["fingerprint"] != fingerprint(decision, observations)
                    or previous["entry_digest"] != entry_text(inputs.entry).digest
                    or previous["expected_cache_revision"] != inputs.revision
                ):
                    raise StaleAtApply("stale_at_apply")
            evaluation = await self._insert_evaluation(
                conn,
                child_id,
                inputs.entry,
                day,
                run["counts"]["origin_main_sha"],
                observations,
                decision,
                True,
                inputs.cache,
            )
            await self.repo.set_count_key(
                conn, run["run_id"], "deferred", f"{identity[0]}:{identity[1]}", None
            )
            if (
                inputs.cache is not None
                and inputs.cache["state"] == decision.state
                and inputs.cache["reason"] == decision.reason
            ):
                return
            await self.repo.append_transition(
                conn, evaluation["id"], inputs, **self._event_values(decision)
            )
            await self.commit_gate(conn)

    @staticmethod
    def _propagation_observations(
        identity: Identity, parents: Sequence[Identity]
    ) -> list[Observation]:
        observations = [
            Observation(
                id=f"propagation:{kind}:{eid}",
                assertion_key=f"propagation:{kind}:{eid}",
                polarity=Polarity.CONTRADICTS,
                source_kind="propagation",
                source_rank=2,
                source_ref=f"{kind}:{eid}",
                propagated_from=str(eid),
                centrality=Centrality.INCIDENTAL,
                temporal=Temporal.DURABLE,
            )
            for kind, eid in sorted(parents, key=lambda pair: (pair[0], str(pair[1])))
        ]
        if not observations:
            observations = [
                Observation(
                    id=f"propagation:lift:{identity[1]}",
                    assertion_key=None,
                    polarity=Polarity.NONE,
                    source_kind="propagation",
                    source_rank=2,
                    source_ref="applied_parent_states",
                    propagated_from=str(identity[1]),
                )
            ]
        return observations

    async def _propagation_decision(
        self,
        conn: AsyncConnection,
        identity: Identity,
        parents: Sequence[Identity],
        *,
        invalidating: bool = False,
    ) -> Decision | None:
        baseline = await self._baseline(conn, identity)
        if baseline.state in ENDING:
            return None
        if invalidating:
            baseline = await self._recomputed(conn, identity)
            if baseline.state in ENDING:
                return None
        decision = decide(
            self._propagation_observations(identity, parents), utc(self.clock.now()).date()
        )
        return replace(baseline, layer="propagation") if decision.state == State.VALID else decision

    async def _prepare_propagation(
        self,
        run: Mapping[str, Any],
        plan: Mapping[Identity, list[Identity]],
        *,
        invalidating: bool = False,
    ) -> tuple[dict[Identity, tuple[Decision, LockedApplyInputs]], list[str]]:
        """Compute the entire sweep before writing any evaluation or event."""
        prepared: dict[Identity, tuple[Decision, LockedApplyInputs]] = {}
        offenders: list[str] = []
        deferrals: list[tuple[Identity, str]] = []
        for identity in sorted(plan, key=lambda pair: (pair[0], str(pair[1]))):
            try:
                async with self.engine.begin() as conn:
                    decision = await self._propagation_decision(
                        conn, identity, plan[identity], invalidating=invalidating
                    )
                    if decision is None:
                        continue
                    if decision.state in ENDING:
                        offenders.append(str(identity[1]))
                        continue
                    inputs = await self.repo.lock_apply_inputs(conn, *identity)
                    prepared[identity] = decision, inputs
            except (StaleAtApply, ApplyDeferred) as exc:
                deferrals.append(
                    (identity, "stale_at_apply" if isinstance(exc, StaleAtApply) else str(exc))
                )
        for (kind, eid), cause in deferrals:
            await self._defer(run["run_id"], kind, str(eid), cause)
        return prepared, offenders

    async def invalidate(
        self,
        run_id: str,
        reason: str,
        backup_proof: BackupProof | None = None,
    ) -> RunReport:
        async with self.run_lock(self.engine) as acquired:
            if not acquired:
                raise PipelineRefusal("run_locked")
            run = await self._read_run(run_id)
            async with self.engine.begin() as conn:
                affected = await self.repo.entries_for_run(conn, run_id)
                has_applied_state = any(
                    [
                        await self.repo.read_cache(conn, *identity) is not None
                        for identity in affected
                    ]
                )
            mutates_state = run["mode"] == Mode.APPLY or has_applied_state
            await self._gate(Mode.APPLY if mutates_state else Mode.DRY_RUN)
            if mutates_state:
                await self._check_backup(run_id, backup_proof, phase="backup_invalidate")
            if mutates_state and not chunk_window_open(self.clock.now()):
                raise PipelineRefusal("apply_window_closed")
            async with self.engine.begin() as conn:
                await self.repo.invalidate_run(conn, run_id, reason)
                child = run["counts"].get("propagation_run_id")
                if child:
                    await self.repo.invalidate_run(conn, child, reason)
                identities = await self.repo.entries_for_run(conn, run_id)
            if not mutates_state:
                return await self.report(run_id)
            identities = sorted(set(identities), key=lambda pair: (pair[0], str(pair[1])))
            # Finalize dependent markers once, after recomputing their parents.
            # 066 allows one compensation per invalidated run and entry.
            propagation_targets = await self._propagation_plan(await self._read_run(run_id))
            for offset in range(0, len(identities), CHUNK_SIZE):
                await self._gate(Mode.APPLY)
                if not chunk_window_open(self.clock.now()):
                    return await self.report(run_id)
                for identity in identities[offset : offset + CHUNK_SIZE]:
                    if identity in propagation_targets:
                        continue
                    try:
                        await self._compensate(run_id, identity)
                    except ApplyWindowClosed:
                        return await self.report(run_id)
                    except (StaleAtApply, ApplyDeferred) as exc:
                        await self._defer(
                            run_id,
                            identity[0],
                            str(identity[1]),
                            "stale_at_apply" if isinstance(exc, StaleAtApply) else str(exc),
                        )
            # Recompute surviving propagation markers from current applied parents.
            # Compensations share the same unique invalidated-run/entry key.
            await self._invalidate_propagation(await self._read_run(run_id), backup_proof)
            return await self.report(run_id)

    async def _recomputed(self, conn: AsyncConnection, identity: Identity) -> Decision:
        history: list[EvaluationRecord] = []
        baseline = Decision(State.VALID, "declaration_only", ())
        baseline_digest: str | None = None
        inputs = await self.repo.lock_apply_inputs(conn, *identity)
        digest = entry_text(inputs.entry).digest
        applied = {
            e["evaluation_id"]
            for e in await self.repo.events_for_entry(conn, *identity)
            if e["kind"] == "transition"
        }
        for row, candidate, item in await self._history(conn, *identity):
            if row["mode"] == Mode.APPLY and candidate.layer != "propagation":
                if confirmed(history, item) and row["complete"] and not row["invalidated"]:
                    run = await self.repo.read_run(conn, row["run_id"])
                    deferred = f"{identity[0]}:{identity[1]}" in run["counts"].get("deferred", {})
                    if row["id"] in applied or (
                        row["run_status"] == RunStatus.APPLIED and not deferred
                    ):
                        baseline = candidate
                        baseline_digest = row["entry_digest"]
            history.append(item)
        if baseline_digest is not None and baseline_digest != digest:
            raise StaleAtApply("stale_at_apply")
        return baseline

    async def _compensate(
        self,
        run_id: str,
        identity: Identity,
        *,
        propagated: Decision | None = None,
        expected: LockedApplyInputs | None = None,
    ) -> None:
        async with self.engine.begin() as conn:
            inputs = await self.repo.lock_apply_inputs(conn, *identity)
            if expected is not None and (
                inputs.entry != expected.entry or inputs.revision != expected.revision
            ):
                raise StaleAtApply("stale_at_apply")
            if inputs.cache is None:
                return
            events = await self.repo.events_for_entry(conn, *identity)
            if any(e.get("invalidated_run_id") == run_id for e in events):
                await self.repo.set_count_key(
                    conn, run_id, "deferred", f"{identity[0]}:{identity[1]}", None
                )
                return
            decision = (
                propagated if propagated is not None else await self._recomputed(conn, identity)
            )
            if (
                inputs.cache["state"] == decision.state
                and inputs.cache["reason"] == decision.reason
            ):
                await self.repo.set_count_key(
                    conn, run_id, "deferred", f"{identity[0]}:{identity[1]}", None
                )
                return
            # The compensated event's complete evaluation supplies entry provenance,
            # even when the recomputed default has no surviving evaluation.
            latest = next(e for e in events if e["id"] == inputs.cache["event_id"])
            evaluation = await self.repo.read_evaluation(conn, latest["evaluation_id"])
            if evaluation["entry_digest"] != entry_text(inputs.entry).digest:
                raise StaleAtApply("stale_at_apply")
            await self.repo.append_compensation(
                conn,
                evaluation_id=latest["evaluation_id"],
                inputs=inputs,
                invalidated_run_id=run_id,
                compensates_event_id=latest["id"],
                expected_cache_revision=inputs.revision,
                **self._event_values(decision),
            )
            await self.repo.set_count_key(
                conn, run_id, "deferred", f"{identity[0]}:{identity[1]}", None
            )
            await self.commit_gate(conn)

    async def _invalidate_propagation(
        self, run: Mapping[str, Any], proof: BackupProof | None
    ) -> None:
        plan = await self._propagation_plan(run)
        prepared, offenders = await self._prepare_propagation(run, plan, invalidating=True)
        if offenders:
            async with self.engine.begin() as conn:
                await self.repo.record_run(
                    conn,
                    run["run_id"],
                    counts={
                        "failure_reason": "propagation_cap_exceeded",
                        "offending_ids": offenders,
                    },
                )
        identities = list(prepared)
        for offset in range(0, len(identities), CHUNK_SIZE):
            await self._gate(Mode.APPLY)
            if not chunk_window_open(self.clock.now()):
                return
            for identity in identities[offset : offset + CHUNK_SIZE]:
                decision, inputs = prepared[identity]
                try:
                    await self._compensate(
                        run["run_id"], identity, propagated=decision, expected=inputs
                    )
                except ApplyWindowClosed:
                    return
                except (StaleAtApply, ApplyDeferred) as exc:
                    await self._defer(
                        run["run_id"],
                        identity[0],
                        str(identity[1]),
                        "stale_at_apply" if isinstance(exc, StaleAtApply) else str(exc),
                    )

    async def report(self, run_id: str) -> RunReport:
        """Build a read-only report from durable counters and evidence, never text."""
        run = await self._read_run(run_id)
        states: Counter[str] = Counter()
        reasons: Counter[str] = Counter()
        pending_states: Counter[str] = Counter()
        pending_reasons: Counter[str] = Counter()
        compensated_states: Counter[str] = Counter()
        compensated_reasons: Counter[str] = Counter()
        seen_events: set[int] = set()
        spots = []
        evaluations = await self._run_evaluations(run)
        child = run["counts"].get("propagation_run_id")
        if child:
            evaluations += await self._run_evaluations(await self._read_run(child))
        async with self.engine.begin() as conn:
            for event in await self.repo.compensations_for_run(conn, run_id):
                compensated_states[event["to_state"]] += 1
                compensated_reasons[event["reason"]] += 1
            for evaluation in evaluations:
                decision, _ = await self._evidence(conn, evaluation)
                entry_events = await self.repo.events_for_entry(
                    conn, evaluation["entity_type"], evaluation["entity_id"]
                )
                events = [
                    e
                    for e in entry_events
                    if e["evaluation_id"] == evaluation["id"] and e["kind"] == "transition"
                ]
                if events:
                    for event in events:
                        if event["id"] in seen_events:
                            continue
                        seen_events.add(event["id"])
                        states[event["to_state"]] += 1
                        reasons[event["reason"]] += 1
                else:
                    history = await self._history(
                        conn, evaluation["entity_type"], evaluation["entity_id"]
                    )
                    prior = [item[2] for item in history if item[0]["id"] < evaluation["id"]]
                    if (
                        evaluation["complete"]
                        and not evaluation.get("invalidated", False)
                        and (
                            run["mode"] == Mode.DRY_RUN
                            or not confirmed(prior, record(evaluation, decision))
                        )
                    ):
                        pending_states[evaluation["state"]] += 1
                        pending_reasons[evaluation["reason"]] += 1
                if decision.layer == "check" and evaluation["state"] in ENDING:
                    spots.append(str(evaluation["entity_id"]))
        metadata = run["counts"]
        backup = metadata.get(
            "backup_invalidate" if run["status"] == RunStatus.INVALIDATED else "backup_apply",
            {},
        )
        end = run["ended_at"] or utc(self.clock.now())
        return RunReport(
            run_id=run_id,
            mode=run["mode"],
            status=run["status"],
            applied_by_state=dict(states),
            applied_by_reason=dict(reasons),
            compensated_by_state=dict(compensated_states),
            compensated_by_reason=dict(compensated_reasons),
            pending_by_state=dict(pending_states),
            pending_by_reason=dict(pending_reasons),
            deferred_by_cause=dict(Counter(metadata.get("deferred", {}).values())),
            screened_entries=[
                e["entity_id"] for e in metadata.get("manifest", {}).get("screened", [])
            ],
            request_statuses=metadata.get("request_statuses", {}),
            real_cost_usd=float(run["real_cost"] or 0),
            token_counts=metadata.get("token_counts", {}),
            worst_case_usd=metadata.get("worst_case_usd", 0),
            usage=run["usage"],
            runtime_seconds=max(0, (utc(end) - utc(run["started_at"])).total_seconds()),
            spot_checks=sorted(set(spots)),
            backup_proof=BACKUP_DECLARATION if run["mode"] == Mode.APPLY or backup else None,
            backup_run_id=backup.get("run_id"),
            backup_sealed_at=backup.get("sealed_at"),
            failure_reason=metadata.get("failure_reason"),
            offending_ids=metadata.get("offending_ids", []),
        )
