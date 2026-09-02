"""PostgreSQL store for bounded plan-index repair operations."""

from __future__ import annotations

import hmac
from collections.abc import Mapping, Sequence
from datetime import UTC, datetime
from typing import TYPE_CHECKING, Any
from uuid import UUID

import sqlalchemy as sa

from brain_v42.db.tables import (
    feature_artifacts,
    indexed_plan_chunks,
    indexed_plans,
    project_contexts,
)
from brain_v42.maintenance.plan_index_repair import (
    TARGET_PROJECT_KEYS,
    ContextRecord,
    FeatureLinkRecord,
    IndexedPlanRecord,
    LocalPlanFile,
    MutationProof,
    PhaseResult,
    ReindexEvidence,
    RepairManifest,
    RepairSafetyError,
    RepairSnapshot,
    VerificationReport,
    VerifiedPlanRecord,
    build_repair_snapshot,
    database_identity_fingerprint,
    sha256_json,
    verify_local_files_unchanged,
)

if TYPE_CHECKING:
    from sqlalchemy.ext.asyncio import AsyncSession, async_sessionmaker

# Bumped to 043 after the review the pin requires (spec §14.7). Unlike 042, 043
# TOUCHES `indexed_plans` — one of the three tables the repair writes — so the
# review could not settle for "it touches nothing".
#
# It adds two nullable columns without a default and a
# `BEFORE UPDATE OF freshness_status` trigger. Measured on this file: the repair
# only does `DELETE` on `indexed_plans` and `UPDATE` on `project_contexts`,
# which is outside 043's scope. No `UPDATE OF freshness_status`, so the trigger
# cannot fire here; no INSERT, so the nullable columns change nothing. The CHECK
# accepts `NULL`. 043 is inert here.
#
# Bumped to 044 after the same review: 044 only adds a nullable column without a
# default, `last_accessed_at_human`, with no trigger and no constraint. It
# touches `indexed_plans` for the same reason 043 does — it is one of the six
# tables tracked by the decay — and stays inert for the same DELETEs.
#
# Bumped to 045 after the same review, the shortest of the three: 045 touches
# none of the three tables the repair writes. Its scope is `dream_runs.model`,
# widened from 30 to 120 characters, plus the DROP/CREATE of the
# `codex_dream_run_v1` view that blocked the ALTER. No trigger, no constraint,
# no NOT NULL column without a default, no row rewritten.
#
# Bumped to 046 after the same review. 046 touches NONE of the three tables the
# repair writes — its scope is `brain_sessions` alone. Verified point by point
# against what the review must forbid: five columns added, ALL nullable and
# without a default; no trigger installed; two CHECKs widened (they accept
# strictly MORE than before, never less, so no existing row can become invalid);
# one PARTIAL UNIQUE index, but on `brain_sessions`, out of scope. No row
# rewritten, no backfill. 046 is inert here.
#
# Bumped to 047 after the same review, and this entry exists because three
# commits bumped the constant to 047, 048 then 049 while this block stayed at 046
# — the failure the closing line below names, realised (ticket 6cc34303). 047
# executes exactly two statements, both on `brain_sessions`: DROP then re-ADD the
# CHECK `brain_sessions_terminal_state_valid`, with the XOR of the `ended` branch
# removed. No table the repair reads or writes. No column, no trigger, no index.
# 047 is inert here.
#
# Bumped to 048 after the same review. Scope: `brain_session_artifacts` alone —
# one nullable VARCHAR(24) without a default (`attribution_mode`), its CHECK
# dropped and re-added, and a PARTIAL index on the derived mode. Same table
# family as 047, none of it in reach of this file. No backfill, so no row
# rewritten. 048 is inert here.
#
# Bumped to 049 after the same review — and this one is NOT inert on the surface,
# the first since 043 that cannot settle for "it touches nothing". 049 widens
# `ck_<table>_freshness_source` on the six decay tables, and `indexed_plans` is
# one of them: DROP CONSTRAINT IF EXISTS then ADD with two more words
# (`manual_update`, `plan_reindex`). Measured against what this file does to that
# table: only `sa.delete(indexed_plans)`. A widened CHECK accepts strictly MORE
# than before, so no existing row can become invalid, and a DELETE cannot violate
# a CHECK at all. The other half of 049 is two nullable `dream_runs` columns,
# out of scope entirely.
#
# 049's `plan_reindex` deserves its own line, because the obvious reading is
# wrong: the repair does NOT have to declare that provenance. It never reindexes.
# It DELETES stale index rows and verifies, through `ReindexEvidence`, that a
# reindex performed elsewhere covered the same snapshot. The re-insertion goes
# through `pg_indexed_plan_repo`, whose upsert already sets
# `freshness_source = 'plan_reindex'` on both its INSERT and its ON CONFLICT
# branches — which is what 049's own docstring says. A provenance written here
# would name the wrong actor for a row this file never creates.
#
# Bumped to 050 after the review the pin requires — and this is the review R1.4
# names, the one the corridor dossier recorded as NOT DONE (AM5), blocking M-D
# from being written at all. 050 lands a `project_focus_history` table plus TWO
# triggers, one of them ON `project_contexts`, which this file UPDATEs. So the
# question is exact: can `project_contexts_focus_history_required` fire on either
# of this file's two UPDATEs?
#
# It cannot, and the reason is the trigger's `OF current_focus` clause. Measured
# on this file, both UPDATEs name exactly two columns:
#
#   line 349-356  .values(plan_scan_paths=..., updated_at=mutation_timestamp)
#   line 619-626  .values(plan_scan_paths=..., updated_at=original_updated_at)
#
# Neither names `current_focus`. PostgreSQL fires an `UPDATE OF col` trigger only
# when `col` appears in the statement's SET list — and it does so on MENTION, not
# on change, which is why the guarantee has to rest on the SET list and not on the
# values. Both statements are built with an explicit `.values(...)`, so a focus
# could only enter by someone adding it here. That is what this block is for.
#
# The append-only trigger is on `project_focus_history`, a table this file never
# reads or writes. The table itself is new, so no row of it can pre-exist. The
# constraint trigger ships DISABLED (`tgenabled = 'D'`) and is armed by an
# operator gesture after the MCP restart: during the window between `upgrade` and
# that gesture, the answer above holds for a second, weaker reason too.
#
# One consequence worth naming rather than discovering: bumping this constant to
# 050 makes the repair REFUSE to run against a database still at 049 — which is
# production, until the operator applies the migration. That is the pin working
# as designed, not a regression, and it is the same regime 049 went through.
#
# Bumped to 051 after its own review, and this one is genuinely short. 051 adds
# `brain_session_checkpoints`: ONE new table, ONE trigger, and both are on that
# table alone. Measured on the two modules rather than assumed — the executed
# surface of this repair is `indexed_plans` (43 mentions), `project_contexts`
# (19) and `indexed_plan_chunks` (13); `brain_sessions` appears four times and
# every one of them is inside a COMMENT of a previous review entry, never in a
# statement. `brain_session_checkpoints` appears nowhere at all.
#
# So neither the append-only trigger (BEFORE UPDATE OR DELETE, on the new table)
# nor the FK's `ON DELETE RESTRICT` (pointing AT `brain_sessions`, from which this
# file deletes nothing) can fire on anything this repair executes. The table is
# also new, so no row of it can pre-exist a run. INERT.
#
# The consequence 050 named applies again and is worth repeating rather than
# assuming inherited: bumping this constant makes the repair REFUSE to run against
# production while production is still at 049. That is the pin working, not a
# regression — and it now spans TWO unapplied revisions instead of one.
#
# The review is written down even when it is short: that is the rule, and a
# missing review reads exactly like a review that was done. Since ticket
# 6cc34303 that rule is enforced rather than trusted:
# `tests/unit/test_plan_index_repair_review_block.py` derives the reviewed set
# from this block and fails if the constant below outruns it, or if a revision
# is skipped between the first entry and the head.
_REQUIRED_ALEMBIC_HEAD = "051"


class RepairStore:
    """Read and later mutate only the rows proven by a private snapshot."""

    def __init__(self, session_factory: async_sessionmaker[AsyncSession]) -> None:
        self._session_factory = session_factory

    async def inventory(
        self,
        manifest: RepairManifest,
        local_files: tuple[LocalPlanFile, ...],
    ) -> RepairSnapshot:
        """Build a complete control snapshot inside one read-only transaction."""
        project_keys = tuple(project.project_key for project in manifest.projects)
        canonical_paths = tuple(item.file_path for item in local_files)
        proposed_paths = {
            project.project_key: tuple(str(path) for path in project.scan_paths)
            for project in manifest.projects
        }

        async with self._session_factory() as session:
            async with session.begin():
                await session.execute(sa.text("SET TRANSACTION READ ONLY"))

                context_result = await session.execute(
                    sa.select(*project_contexts.c)
                    .where(project_contexts.c.project_key.in_(project_keys))
                    .order_by(project_contexts.c.project_key)
                )
                context_rows = context_result.mappings().all()
                if {row["project_key"] for row in context_rows} != set(project_keys):
                    raise RepairSafetyError("context_set_mismatch")

                plan_result = await session.execute(
                    sa.select(
                        indexed_plans.c.id,
                        indexed_plans.c.project_key,
                        indexed_plans.c.file_path,
                        indexed_plans.c.content_hash,
                        indexed_plans.c.status,
                        indexed_plans.c.freshness_status,
                        indexed_plans.c.chunk_count,
                    )
                    .where(
                        sa.or_(
                            indexed_plans.c.project_key.in_(project_keys),
                            indexed_plans.c.file_path.in_(canonical_paths),
                        )
                    )
                    .order_by(indexed_plans.c.id)
                )
                plan_rows = plan_result.mappings().all()
                plan_ids = tuple(row["id"] for row in plan_rows)

                chunk_result = await session.execute(
                    sa.select(
                        indexed_plan_chunks.c.plan_id,
                        sa.func.count().label("observed_chunk_count"),
                    )
                    .where(indexed_plan_chunks.c.plan_id.in_(plan_ids))
                    .group_by(indexed_plan_chunks.c.plan_id)
                    .order_by(indexed_plan_chunks.c.plan_id)
                )
                chunk_counts = {
                    row["plan_id"]: int(row["observed_chunk_count"])
                    for row in chunk_result.mappings().all()
                }

                link_result = await session.execute(
                    sa.select(
                        feature_artifacts.c.feature_id,
                        feature_artifacts.c.artifact_id,
                        feature_artifacts.c.similarity_score,
                        feature_artifacts.c.created_at,
                    )
                    .where(feature_artifacts.c.artifact_type == "plan")
                    .where(feature_artifacts.c.artifact_id.in_(plan_ids))
                    .order_by(
                        feature_artifacts.c.artifact_id,
                        feature_artifacts.c.feature_id,
                    )
                )
                link_rows = link_result.mappings().all()

                alembic_result = await session.execute(
                    sa.text("SELECT version_num FROM alembic_version")
                )
                alembic_revision = str(alembic_result.scalar_one())

                identity_result = await session.execute(
                    sa.text(
                        "SELECT current_database() AS database_name, "
                        "inet_server_addr()::text AS server_address, "
                        "inet_server_port() AS server_port"
                    )
                )
                identity = dict(identity_result.mappings().one())

        contexts = tuple(
            ContextRecord.from_values(
                dict(row),
                proposed_plan_scan_paths=proposed_paths[str(row["project_key"])],
            )
            for row in context_rows
        )
        plans = tuple(
            IndexedPlanRecord(
                id=str(row["id"]),
                project_key=str(row["project_key"]),
                file_path=str(row["file_path"]),
                content_hash=str(row["content_hash"]),
                status=str(row["status"]),
                freshness_status=str(row["freshness_status"]),
                declared_chunk_count=int(row["chunk_count"]),
                observed_chunk_count=chunk_counts.get(row["id"], 0),
            )
            for row in plan_rows
        )
        links = tuple(
            FeatureLinkRecord(
                feature_id=str(row["feature_id"]),
                plan_id=str(row["artifact_id"]),
                similarity_score=float(row["similarity_score"]),
                created_at=_stable_text(row["created_at"]),
            )
            for row in link_rows
        )
        return build_repair_snapshot(
            manifest=manifest,
            local_files=local_files,
            contexts=contexts,
            plans=plans,
            feature_links=links,
            alembic_revision=alembic_revision,
            database_identity_hash=database_identity_fingerprint(identity),
            mutation_timestamp=datetime.now(UTC).isoformat(),
        )

    async def apply_paths(
        self,
        snapshot: RepairSnapshot,
        proof: MutationProof,
    ) -> PhaseResult:
        """Apply only signed canonical paths after complete transactional CAS checks."""
        if proof.postgres_restore_tested is not True:
            raise RepairSafetyError("postgres_restore_not_tested")
        if proof.writers_off_confirmed is not True:
            raise RepairSafetyError("writers_off_not_confirmed")
        snapshot_digest = sha256_json(snapshot.to_dict())
        if not hmac.compare_digest(snapshot_digest, proof.snapshot_sha256):
            raise RepairSafetyError("snapshot_proof_mismatch")

        contexts_by_key = {context.project_key: context for context in snapshot.contexts}
        if len(contexts_by_key) != len(snapshot.contexts):
            raise RepairSafetyError("context_cas_conflict")
        if set(contexts_by_key) != TARGET_PROJECT_KEYS:
            raise RepairSafetyError("context_set_mismatch")
        project_keys = tuple(sorted(TARGET_PROJECT_KEYS))
        mutation_timestamp = datetime.fromisoformat(snapshot.mutation_timestamp)

        transaction_failed = False
        try:
            async with self._session_factory() as session:
                async with session.begin():
                    await session.execute(sa.text("SET TRANSACTION ISOLATION LEVEL SERIALIZABLE"))

                    identity_result = await session.execute(
                        sa.text(
                            "SELECT current_database() AS database_name, "
                            "inet_server_addr()::text AS server_address, "
                            "inet_server_port() AS server_port"
                        )
                    )
                    identity = dict(identity_result.mappings().one())
                    current_identity_hash = database_identity_fingerprint(identity)
                    if not hmac.compare_digest(
                        current_identity_hash,
                        snapshot.database_identity_hash,
                    ):
                        raise RepairSafetyError("database_identity_mismatch")

                    alembic_result = await session.execute(
                        sa.text("SELECT version_num FROM alembic_version")
                    )
                    alembic_head = str(alembic_result.scalar_one())
                    if (
                        snapshot.alembic_revision != _REQUIRED_ALEMBIC_HEAD
                        or alembic_head != snapshot.alembic_revision
                    ):
                        raise RepairSafetyError("alembic_head_mismatch")

                    context_result = await session.execute(
                        sa.select(*project_contexts.c)
                        .where(project_contexts.c.project_key.in_(project_keys))
                        .order_by(project_contexts.c.project_key)
                        .with_for_update()
                    )
                    locked_rows = context_result.mappings().all()
                    if len(locked_rows) != len(project_keys) or {
                        str(row["project_key"]) for row in locked_rows
                    } != set(project_keys):
                        raise RepairSafetyError("context_cas_conflict")

                    row_states: dict[str, str] = {}
                    for row in locked_rows:
                        project_key = str(row["project_key"])
                        original = contexts_by_key[project_key]
                        current_fingerprint = ContextRecord.from_values(
                            dict(row),
                            proposed_plan_scan_paths=original.proposed_plan_scan_paths,
                        ).fingerprint
                        expected_values = dict(original.values)
                        expected_values["plan_scan_paths"] = list(original.proposed_plan_scan_paths)
                        expected_values["updated_at"] = snapshot.mutation_timestamp
                        expected_fingerprint = sha256_json(expected_values)

                        if hmac.compare_digest(current_fingerprint, original.fingerprint):
                            row_states[project_key] = "original"
                        elif hmac.compare_digest(current_fingerprint, expected_fingerprint):
                            row_states[project_key] = "post_update"
                        else:
                            raise RepairSafetyError("context_cas_conflict")

                    states = set(row_states.values())
                    if states == {"post_update"}:
                        return PhaseResult(status="already_applied", affected_rows=0)
                    if states != {"original"}:
                        raise RepairSafetyError("context_cas_conflict")

                    await session.execute(
                        sa.text(
                            "SET LOCAL brain_v42.allow_explicit_project_context_updated_at = 'on'"
                        )
                    )
                    for project_key in project_keys:
                        context = contexts_by_key[project_key]
                        update_result = await session.execute(
                            sa.update(project_contexts)
                            .where(project_contexts.c.project_key == project_key)
                            .values(
                                plan_scan_paths=list(context.proposed_plan_scan_paths),
                                updated_at=mutation_timestamp,
                            )
                            .returning(project_contexts.c.updated_at)
                        )
                        returned_timestamps = update_result.scalars().all()
                        if len(returned_timestamps) != 1:
                            raise RepairSafetyError("context_update_count_mismatch")
                        if returned_timestamps[0] != mutation_timestamp:
                            raise RepairSafetyError("context_cas_conflict")
        except RepairSafetyError:
            raise
        except Exception:
            transaction_failed = True

        if transaction_failed:
            raise RepairSafetyError("apply_paths_transaction_failed") from None

        return PhaseResult(status="applied", affected_rows=len(project_keys))

    async def verify(
        self,
        snapshot: RepairSnapshot,
        evidence: ReindexEvidence,
    ) -> VerificationReport:
        """Verify the exact canonical corpus and bind it to reindex evidence."""
        snapshot_digest = sha256_json(snapshot.to_dict())
        if not hmac.compare_digest(snapshot_digest, evidence.snapshot_sha256):
            raise RepairSafetyError("snapshot_evidence_mismatch")
        _contexts_by_key(snapshot)
        verify_local_files_unchanged(snapshot.local_files)

        report: VerificationReport | None = None
        transaction_failed = False
        try:
            async with self._session_factory() as session:
                async with session.begin():
                    await session.execute(
                        sa.text("SET TRANSACTION ISOLATION LEVEL SERIALIZABLE READ ONLY")
                    )
                    await _validate_transaction_target(session, snapshot)
                    plan_result = await session.execute(_scoped_plan_select(snapshot))
                    plan_rows = plan_result.mappings().all()
                    plan_ids = tuple(row["id"] for row in plan_rows)

                    chunk_result = await session.execute(
                        sa.select(
                            indexed_plan_chunks.c.plan_id,
                            sa.func.count().label("observed_chunk_count"),
                        )
                        .where(indexed_plan_chunks.c.plan_id.in_(plan_ids))
                        .group_by(indexed_plan_chunks.c.plan_id)
                        .order_by(indexed_plan_chunks.c.plan_id)
                    )
                    chunk_counts = {
                        str(row["plan_id"]): int(row["observed_chunk_count"])
                        for row in chunk_result.mappings().all()
                    }
                    link_result = await session.execute(
                        sa.select(
                            feature_artifacts.c.feature_id,
                            feature_artifacts.c.artifact_id,
                        )
                        .where(feature_artifacts.c.artifact_type == "plan")
                        .where(feature_artifacts.c.artifact_id.in_(plan_ids))
                        .order_by(
                            feature_artifacts.c.artifact_id,
                            feature_artifacts.c.feature_id,
                        )
                    )
                    link_rows = link_result.mappings().all()
                    canonical_plans = _verify_current_corpus(
                        snapshot,
                        evidence,
                        plan_rows,
                        chunk_counts,
                        link_rows,
                    )
                    report = VerificationReport(
                        version=1,
                        snapshot_sha256=snapshot_digest,
                        evidence_sha256=sha256_json(evidence.to_dict()),
                        evidence=evidence,
                        canonical_plans=canonical_plans,
                    )
        except RepairSafetyError:
            raise
        except Exception:
            transaction_failed = True

        if transaction_failed:
            raise RepairSafetyError("verify_transaction_failed") from None
        if report is None:
            raise RepairSafetyError("verify_transaction_failed")
        return report

    async def finalize(
        self,
        snapshot: RepairSnapshot,
        proof: MutationProof,
        report: VerificationReport,
    ) -> PhaseResult:
        """Delete only snapshotted polluted rows after repeating every CAS gate."""
        _validate_mutation_proof(snapshot, proof)
        _validate_verification_report(snapshot, report)
        polluted_plans = _polluted_snapshot_plans(snapshot)
        polluted_ids = tuple(_database_uuid(plan_id) for plan_id in polluted_plans)
        expected_link_count = sum(link.plan_id in polluted_plans for link in snapshot.feature_links)

        transaction_failed = False
        try:
            async with self._session_factory() as session:
                async with session.begin():
                    await session.execute(sa.text("SET TRANSACTION ISOLATION LEVEL SERIALIZABLE"))
                    await _validate_transaction_target(session, snapshot)
                    context_result = await session.execute(_locked_context_select())
                    if (
                        _context_rows_state(
                            snapshot,
                            context_result.mappings().all(),
                        )
                        != "post_update"
                    ):
                        raise RepairSafetyError("context_cas_conflict")

                    _validate_verification_report(snapshot, report)
                    plan_result = await session.execute(_scoped_plan_select(snapshot, lock=True))
                    plan_rows = plan_result.mappings().all()
                    chunk_result = await session.execute(
                        sa.select(
                            indexed_plan_chunks.c.plan_id,
                            sa.func.count().label("observed_chunk_count"),
                        )
                        .where(indexed_plan_chunks.c.plan_id.in_(polluted_ids))
                        .group_by(indexed_plan_chunks.c.plan_id)
                        .order_by(indexed_plan_chunks.c.plan_id)
                    )
                    chunk_counts = {
                        str(row["plan_id"]): int(row["observed_chunk_count"])
                        for row in chunk_result.mappings().all()
                    }
                    link_result = await session.execute(
                        sa.select(
                            feature_artifacts.c.feature_id,
                            feature_artifacts.c.artifact_id,
                            feature_artifacts.c.similarity_score,
                            feature_artifacts.c.created_at,
                        )
                        .where(feature_artifacts.c.artifact_type == "plan")
                        .where(feature_artifacts.c.artifact_id.in_(polluted_ids))
                        .order_by(
                            feature_artifacts.c.artifact_id,
                            feature_artifacts.c.feature_id,
                        )
                    )
                    _validate_finalize_plan_rows(
                        snapshot,
                        report,
                        plan_rows,
                        chunk_counts,
                        link_result.mappings().all(),
                    )
                    verify_local_files_unchanged(snapshot.local_files)

                    link_delete = await session.execute(
                        sa.delete(feature_artifacts)
                        .where(feature_artifacts.c.artifact_type == "plan")
                        .where(feature_artifacts.c.artifact_id.in_(polluted_ids))
                    )
                    if getattr(link_delete, "rowcount", None) != expected_link_count:
                        raise RepairSafetyError("feature_link_delete_count_mismatch")

                    plan_delete = await session.execute(
                        sa.delete(indexed_plans).where(
                            _exact_plan_predicate(tuple(polluted_plans.values()))
                        )
                    )
                    if getattr(plan_delete, "rowcount", None) != len(polluted_plans):
                        raise RepairSafetyError("polluted_plan_delete_count_mismatch")
        except RepairSafetyError:
            raise
        except Exception:
            transaction_failed = True

        if transaction_failed:
            raise RepairSafetyError("finalize_transaction_failed") from None
        return PhaseResult(status="finalized", affected_rows=len(polluted_plans))

    async def rollback_before_finalize(
        self,
        snapshot: RepairSnapshot,
        proof: MutationProof,
    ) -> PhaseResult:
        """Restore apply-only fields and delete only proven new canonical rows."""
        _validate_mutation_proof(snapshot, proof)
        result: PhaseResult | None = None
        transaction_failed = False
        try:
            async with self._session_factory() as session:
                async with session.begin():
                    await session.execute(sa.text("SET TRANSACTION ISOLATION LEVEL SERIALIZABLE"))
                    await _validate_transaction_target(session, snapshot)
                    context_result = await session.execute(_locked_context_select())
                    context_state = _context_rows_state(
                        snapshot,
                        context_result.mappings().all(),
                    )

                    plan_result = await session.execute(_scoped_plan_select(snapshot, lock=True))
                    plan_state, new_plan_rows = _classify_rollback_plan_rows(
                        snapshot,
                        plan_result.mappings().all(),
                    )
                    if plan_state == "post_finalize":
                        result = PhaseResult(
                            status="backup_restore_required",
                            affected_rows=0,
                            backup_receipt_sha256=proof.backup_receipt_sha256,
                        )
                    else:
                        verify_local_files_unchanged(snapshot.local_files)
                        if context_state == "original" and plan_state in {
                            "absent",
                            "empty",
                        }:
                            result = PhaseResult(
                                status="already_rolled_back",
                                affected_rows=0,
                            )
                        elif context_state != "post_update" or plan_state not in {
                            "present",
                            "empty",
                        }:
                            raise RepairSafetyError("rollback_state_conflict")
                        else:
                            new_plan_ids = tuple(_database_uuid(row["id"]) for row in new_plan_rows)
                            expected_link_count = 0
                            if new_plan_ids:
                                link_result = await session.execute(
                                    sa.select(
                                        feature_artifacts.c.feature_id,
                                        feature_artifacts.c.artifact_id,
                                    )
                                    .where(feature_artifacts.c.artifact_type == "plan")
                                    .where(feature_artifacts.c.artifact_id.in_(new_plan_ids))
                                    .order_by(
                                        feature_artifacts.c.artifact_id,
                                        feature_artifacts.c.feature_id,
                                    )
                                    .with_for_update()
                                )
                                expected_link_count = len(link_result.mappings().all())

                            contexts_by_key = _contexts_by_key(snapshot)
                            await session.execute(
                                sa.text(
                                    "SET LOCAL brain_v42.allow_explicit_project_context_updated_at = 'on'"
                                )
                            )
                            for project_key in sorted(contexts_by_key):
                                context = contexts_by_key[project_key]
                                original_updated_at = context.values["updated_at"]
                                if isinstance(original_updated_at, str):
                                    original_updated_at = datetime.fromisoformat(
                                        original_updated_at
                                    )
                                update_result = await session.execute(
                                    sa.update(project_contexts)
                                    .where(project_contexts.c.project_key == project_key)
                                    .values(
                                        plan_scan_paths=list(context.values["plan_scan_paths"]),
                                        updated_at=original_updated_at,
                                    )
                                    .returning(project_contexts.c.updated_at)
                                )
                                returned_timestamps = update_result.scalars().all()
                                if len(returned_timestamps) != 1:
                                    raise RepairSafetyError("context_update_count_mismatch")
                                if returned_timestamps[0] != original_updated_at:
                                    raise RepairSafetyError("context_cas_conflict")

                            if new_plan_ids:
                                link_delete = await session.execute(
                                    sa.delete(feature_artifacts)
                                    .where(feature_artifacts.c.artifact_type == "plan")
                                    .where(feature_artifacts.c.artifact_id.in_(new_plan_ids))
                                )
                                if getattr(link_delete, "rowcount", None) != expected_link_count:
                                    raise RepairSafetyError("feature_link_delete_count_mismatch")

                                plan_delete = await session.execute(
                                    sa.delete(indexed_plans).where(
                                        _exact_plan_predicate(new_plan_rows)
                                    )
                                )
                                if getattr(plan_delete, "rowcount", None) != len(new_plan_rows):
                                    raise RepairSafetyError("canonical_plan_delete_count_mismatch")
                            result = PhaseResult(
                                status="rolled_back",
                                affected_rows=len(contexts_by_key) + len(new_plan_rows),
                            )
        except RepairSafetyError:
            raise
        except Exception:
            transaction_failed = True

        if transaction_failed:
            raise RepairSafetyError("rollback_transaction_failed") from None
        if result is None:
            raise RepairSafetyError("rollback_transaction_failed")
        return result


def _stable_text(value: Any) -> str:
    if isinstance(value, datetime):
        if value.tzinfo is None:
            raise RepairSafetyError("naive_datetime")
        return value.astimezone(UTC).isoformat()
    return str(value)


def _validate_mutation_proof(
    snapshot: RepairSnapshot,
    proof: MutationProof,
) -> None:
    if proof.postgres_restore_tested is not True:
        raise RepairSafetyError("postgres_restore_not_tested")
    if proof.writers_off_confirmed is not True:
        raise RepairSafetyError("writers_off_not_confirmed")
    if not hmac.compare_digest(sha256_json(snapshot.to_dict()), proof.snapshot_sha256):
        raise RepairSafetyError("snapshot_proof_mismatch")
    _contexts_by_key(snapshot)


def _contexts_by_key(snapshot: RepairSnapshot) -> dict[str, ContextRecord]:
    contexts = {context.project_key: context for context in snapshot.contexts}
    if len(contexts) != len(snapshot.contexts):
        raise RepairSafetyError("context_cas_conflict")
    if set(contexts) != TARGET_PROJECT_KEYS:
        raise RepairSafetyError("context_set_mismatch")
    return contexts


def _locked_context_select() -> sa.sql.Select[Any]:
    return (
        sa.select(*project_contexts.c)
        .where(project_contexts.c.project_key.in_(tuple(sorted(TARGET_PROJECT_KEYS))))
        .order_by(project_contexts.c.project_key)
        .with_for_update()
    )


def _context_rows_state(snapshot: RepairSnapshot, rows: Sequence[Any]) -> str:
    contexts = _contexts_by_key(snapshot)
    if len(rows) != len(contexts) or {str(row["project_key"]) for row in rows} != set(contexts):
        raise RepairSafetyError("context_cas_conflict")

    states: set[str] = set()
    for row in rows:
        project_key = str(row["project_key"])
        expected = contexts[project_key]
        current_fingerprint = ContextRecord.from_values(
            dict(row),
            proposed_plan_scan_paths=expected.proposed_plan_scan_paths,
        ).fingerprint
        post_values = dict(expected.values)
        post_values["plan_scan_paths"] = list(expected.proposed_plan_scan_paths)
        post_values["updated_at"] = snapshot.mutation_timestamp
        post_fingerprint = sha256_json(post_values)
        if hmac.compare_digest(current_fingerprint, expected.fingerprint):
            states.add("original")
        elif hmac.compare_digest(current_fingerprint, post_fingerprint):
            states.add("post_update")
        else:
            raise RepairSafetyError("context_cas_conflict")
    if len(states) != 1:
        raise RepairSafetyError("context_cas_conflict")
    return states.pop()


async def _validate_transaction_target(
    session: AsyncSession,
    snapshot: RepairSnapshot,
) -> None:
    identity_result = await session.execute(
        sa.text(
            "SELECT current_database() AS database_name, "
            "inet_server_addr()::text AS server_address, "
            "inet_server_port() AS server_port"
        )
    )
    current_identity_hash = database_identity_fingerprint(dict(identity_result.mappings().one()))
    if not hmac.compare_digest(
        current_identity_hash,
        snapshot.database_identity_hash,
    ):
        raise RepairSafetyError("database_identity_mismatch")

    alembic_result = await session.execute(sa.text("SELECT version_num FROM alembic_version"))
    alembic_head = str(alembic_result.scalar_one())
    if (
        snapshot.alembic_revision != _REQUIRED_ALEMBIC_HEAD
        or alembic_head != snapshot.alembic_revision
    ):
        raise RepairSafetyError("alembic_head_mismatch")


def _scoped_plan_select(
    snapshot: RepairSnapshot,
    *,
    lock: bool = False,
) -> sa.sql.Select[Any]:
    statement = (
        sa.select(
            indexed_plans.c.id,
            indexed_plans.c.project_key,
            indexed_plans.c.file_path,
            indexed_plans.c.content_hash,
            indexed_plans.c.status,
            indexed_plans.c.freshness_status,
            indexed_plans.c.chunk_count,
        )
        .where(
            sa.or_(
                indexed_plans.c.project_key.in_(tuple(sorted(TARGET_PROJECT_KEYS))),
                indexed_plans.c.file_path.in_(
                    tuple(item.file_path for item in snapshot.local_files)
                ),
                indexed_plans.c.id.in_(
                    tuple(_database_uuid(plan_id) for plan_id in snapshot.polluted_plan_ids)
                ),
            )
        )
        .order_by(indexed_plans.c.id)
    )
    return statement.with_for_update() if lock else statement


def _polluted_snapshot_plans(
    snapshot: RepairSnapshot,
) -> dict[str, IndexedPlanRecord]:
    polluted_ids = set(snapshot.polluted_plan_ids)
    plans = {plan.id: plan for plan in snapshot.indexed_plans if plan.id in polluted_ids}
    if len(plans) != len(polluted_ids):
        raise RepairSafetyError("polluted_plan_changed")
    return plans


def _canonical_snapshot_plans(
    snapshot: RepairSnapshot,
) -> dict[str, IndexedPlanRecord]:
    local_tuples = {
        (item.project_key, item.file_path, item.content_hash) for item in snapshot.local_files
    }
    plans = {
        plan.file_path: plan
        for plan in snapshot.indexed_plans
        if (plan.project_key, plan.file_path, plan.content_hash) in local_tuples
    }
    if len(plans) != sum(
        (plan.project_key, plan.file_path, plan.content_hash) in local_tuples
        for plan in snapshot.indexed_plans
    ):
        raise RepairSafetyError("canonical_plan_set_mismatch")
    return plans


def _verify_current_corpus(
    snapshot: RepairSnapshot,
    evidence: ReindexEvidence,
    rows: Sequence[Any],
    chunk_counts: dict[str, int],
    link_rows: Sequence[Any],
) -> tuple[VerifiedPlanRecord, ...]:
    local_by_path = {item.file_path: item for item in snapshot.local_files}
    if len(local_by_path) != len(snapshot.local_files):
        raise RepairSafetyError("canonical_plan_set_mismatch")
    canonical_rows = [row for row in rows if str(row["file_path"]) in local_by_path]
    canonical_paths = [str(row["file_path"]) for row in canonical_rows]
    for row in canonical_rows:
        expected_local = local_by_path[str(row["file_path"])]
        if str(row["project_key"]) != expected_local.project_key:
            raise RepairSafetyError("canonical_plan_owner_mismatch")
        if not hmac.compare_digest(str(row["content_hash"]), expected_local.content_hash):
            raise RepairSafetyError("canonical_plan_hash_mismatch")
    if len(canonical_rows) != len(local_by_path) or set(canonical_paths) != set(local_by_path):
        raise RepairSafetyError("canonical_plan_set_mismatch")

    polluted = _polluted_snapshot_plans(snapshot)
    rows_by_id: dict[str, Any] = {}
    for row in rows:
        row_id = str(row["id"])
        if row_id in rows_by_id:
            raise RepairSafetyError("canonical_plan_set_mismatch")
        rows_by_id[row_id] = row
        if str(row["file_path"]) not in local_by_path and row_id not in polluted:
            raise RepairSafetyError("polluted_plan_changed")
    if not set(polluted).issubset(rows_by_id):
        raise RepairSafetyError("polluted_plan_changed")
    for plan_id, expected_polluted in polluted.items():
        row = rows_by_id[plan_id]
        if (
            str(row["project_key"]) != expected_polluted.project_key
            or str(row["file_path"]) != expected_polluted.file_path
            or str(row["content_hash"]) != expected_polluted.content_hash
            or str(row["status"]) != expected_polluted.status
            or str(row["freshness_status"]) != expected_polluted.freshness_status
            or int(row["chunk_count"]) != expected_polluted.declared_chunk_count
            or chunk_counts.get(plan_id, 0) != expected_polluted.observed_chunk_count
        ):
            raise RepairSafetyError("polluted_plan_changed")

    original_canonical = _canonical_snapshot_plans(snapshot)
    missing_paths = {item.file_path for item in snapshot.missing_canonical_files}
    canonical_by_path = {str(row["file_path"]): row for row in canonical_rows}
    new_ids = {str(row["id"]) for path, row in canonical_by_path.items() if path in missing_paths}
    links_by_project = dict.fromkeys(TARGET_PROJECT_KEYS, 0)
    ids_to_project = {str(row["id"]): str(row["project_key"]) for row in canonical_rows}
    for link in link_rows:
        plan_id = str(link["artifact_id"])
        if plan_id in new_ids:
            links_by_project[ids_to_project[plan_id]] += 1

    evidence_by_project = {project.project_key: project for project in evidence.projects}
    for project_key in TARGET_PROJECT_KEYS:
        project_missing = [
            item for item in snapshot.missing_canonical_files if item.project_key == project_key
        ]
        project_existing = [
            plan for plan in original_canonical.values() if plan.project_key == project_key
        ]
        project_new_ids = {str(canonical_by_path[item.file_path]["id"]) for item in project_missing}
        expected_stats = (
            len(project_missing),
            len(project_existing),
            links_by_project[project_key],
            sum(chunk_counts.get(plan_id, 0) for plan_id in project_new_ids),
        )
        actual = evidence_by_project[project_key]
        if (
            actual.indexed,
            actual.skipped,
            actual.linked,
            actual.chunks_created,
        ) != expected_stats:
            raise RepairSafetyError("reindex_stats_mismatch")

    return tuple(
        VerifiedPlanRecord(
            id=str(row["id"]),
            project_key=str(row["project_key"]),
            file_path=str(row["file_path"]),
            content_hash=str(row["content_hash"]),
        )
        for row in canonical_rows
    )


def _validate_verification_report(
    snapshot: RepairSnapshot,
    report: VerificationReport,
) -> None:
    snapshot_digest = sha256_json(snapshot.to_dict())
    if not hmac.compare_digest(snapshot_digest, report.snapshot_sha256):
        raise RepairSafetyError("snapshot_report_mismatch")
    if not hmac.compare_digest(
        report.evidence.snapshot_sha256,
        report.snapshot_sha256,
    ) or not hmac.compare_digest(
        sha256_json(report.evidence.to_dict()),
        report.evidence_sha256,
    ):
        raise RepairSafetyError("verification_report_mismatch")
    expected_tuples = {
        (item.project_key, item.file_path, item.content_hash) for item in snapshot.local_files
    }
    report_tuples = {
        (plan.project_key, plan.file_path, plan.content_hash) for plan in report.canonical_plans
    }
    if report_tuples != expected_tuples or len(report.canonical_plans) != len(expected_tuples):
        raise RepairSafetyError("verification_report_mismatch")


def _validate_finalize_plan_rows(
    snapshot: RepairSnapshot,
    report: VerificationReport,
    rows: Sequence[Any],
    chunk_counts: Mapping[str, int],
    link_rows: Sequence[Any],
) -> None:
    expected: dict[str, tuple[str, str, str]] = {
        plan.id: (plan.project_key, plan.file_path, plan.content_hash)
        for plan in report.canonical_plans
    }
    for plan_id, plan in _polluted_snapshot_plans(snapshot).items():
        if plan_id in expected:
            raise RepairSafetyError("finalize_plan_cas_conflict")
        expected[plan_id] = (plan.project_key, plan.file_path, plan.content_hash)
    current = {
        str(row["id"]): (
            str(row["project_key"]),
            str(row["file_path"]),
            str(row["content_hash"]),
        )
        for row in rows
    }
    if len(current) != len(rows) or current != expected:
        raise RepairSafetyError("finalize_plan_cas_conflict")

    polluted = _polluted_snapshot_plans(snapshot)
    current_by_id = {str(row["id"]): row for row in rows}
    for plan_id, expected_plan in polluted.items():
        row = current_by_id[plan_id]
        if (
            str(row["status"]) != expected_plan.status
            or str(row["freshness_status"]) != expected_plan.freshness_status
            or int(row["chunk_count"]) != expected_plan.declared_chunk_count
            or chunk_counts.get(plan_id, 0) != expected_plan.observed_chunk_count
        ):
            raise RepairSafetyError("finalize_plan_cas_conflict")

    expected_links = sorted(
        (
            link.feature_id,
            link.plan_id,
            link.similarity_score,
            link.created_at,
        )
        for link in snapshot.feature_links
        if link.plan_id in polluted
    )
    current_links = sorted(
        (
            str(row["feature_id"]),
            str(row["artifact_id"]),
            float(row["similarity_score"]),
            _stable_text(row["created_at"]),
        )
        for row in link_rows
    )
    if current_links != expected_links:
        raise RepairSafetyError("finalize_plan_cas_conflict")


def _classify_rollback_plan_rows(
    snapshot: RepairSnapshot,
    rows: Sequence[Any],
) -> tuple[str, tuple[Any, ...]]:
    local_by_path = {item.file_path: item for item in snapshot.local_files}
    if len(local_by_path) != len(snapshot.local_files):
        raise RepairSafetyError("canonical_plan_set_mismatch")
    polluted = _polluted_snapshot_plans(snapshot)
    rows_by_id = {str(row["id"]): row for row in rows}
    if len(rows_by_id) != len(rows):
        raise RepairSafetyError("polluted_plan_changed")

    current_polluted_ids = set(rows_by_id).intersection(polluted)
    for row_id, row in rows_by_id.items():
        if str(row["file_path"]) not in local_by_path and row_id not in polluted:
            raise RepairSafetyError("polluted_plan_changed")
    if polluted and not current_polluted_ids:
        return "post_finalize", ()
    if current_polluted_ids != set(polluted):
        raise RepairSafetyError("polluted_plan_missing")
    for plan_id, expected_polluted in polluted.items():
        row = rows_by_id[plan_id]
        if (
            str(row["project_key"]) != expected_polluted.project_key
            or str(row["file_path"]) != expected_polluted.file_path
            or str(row["content_hash"]) != expected_polluted.content_hash
        ):
            raise RepairSafetyError("polluted_plan_changed")

    canonical_rows = [row for row in rows if str(row["file_path"]) in local_by_path]
    canonical_by_path = {str(row["file_path"]): row for row in canonical_rows}
    if len(canonical_by_path) != len(canonical_rows):
        raise RepairSafetyError("canonical_plan_set_mismatch")
    for path, row in canonical_by_path.items():
        expected_local = local_by_path[path]
        if (
            str(row["project_key"]) != expected_local.project_key
            or str(row["content_hash"]) != expected_local.content_hash
        ):
            raise RepairSafetyError("canonical_plan_set_mismatch")

    original = _canonical_snapshot_plans(snapshot)
    for path, plan in original.items():
        row = canonical_by_path.get(path)
        if row is None or (
            str(row["id"]) != plan.id
            or str(row["project_key"]) != plan.project_key
            or str(row["content_hash"]) != plan.content_hash
        ):
            raise RepairSafetyError("canonical_plan_set_mismatch")

    missing_paths = {item.file_path for item in snapshot.missing_canonical_files}
    new_rows = tuple(
        canonical_by_path[path] for path in sorted(missing_paths) if path in canonical_by_path
    )
    if not missing_paths:
        return "empty", ()
    if len(new_rows) == len(missing_paths):
        return "present", new_rows
    if not new_rows:
        return "absent", ()
    raise RepairSafetyError("canonical_plan_set_mismatch")


def _exact_plan_predicate(plans: tuple[Any, ...]) -> Any:
    predicates = []
    for plan in plans:
        if isinstance(plan, IndexedPlanRecord):
            plan_id = _database_uuid(plan.id)
            project_key = plan.project_key
            file_path = plan.file_path
            content_hash = plan.content_hash
        else:
            plan_id = _database_uuid(plan["id"])
            project_key = str(plan["project_key"])
            file_path = str(plan["file_path"])
            content_hash = str(plan["content_hash"])
        predicates.append(
            sa.and_(
                indexed_plans.c.id == plan_id,
                indexed_plans.c.project_key == project_key,
                indexed_plans.c.file_path == file_path,
                indexed_plans.c.content_hash == content_hash,
            )
        )
    if not predicates:
        return sa.false()
    return sa.or_(*predicates)


def _database_uuid(value: object) -> UUID:
    if isinstance(value, UUID):
        return value
    try:
        return UUID(str(value))
    except (AttributeError, TypeError, ValueError):
        raise RepairSafetyError("invalid_snapshot_schema") from None
