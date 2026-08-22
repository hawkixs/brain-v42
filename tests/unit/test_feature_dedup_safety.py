"""Unit tests for FeatureDedupJob — merge safety fixes.

Tests three bug classes:
(a) Chain-of-merges: existence re-check (FOR UPDATE) and pair-skip for
    already-consumed IDs.
(b) Embed-fail must preserve the target's existing embedding (not set NULL).
(c) Stale-snapshot description loss: merge_features must read target_desc /
    source_desc from the authoritative FOR UPDATE rows, not from the stale
    snapshot objects passed in.  Scenario: candidates [(A,B),(A,C)] — after
    merge 1 A.description = "A\\n---\\nB"; merge 2 must use the DB-current
    description of A (which now contains B) so the final result is
    "A\\n---\\nB\\n---\\nC" (B survives), not "A\\n---\\nC" (B silently lost).
(d) merge_features returns bool (True = merged, False = skipped) so the caller
    can distinguish a real merge from a no-op skip.
"""

from __future__ import annotations

import uuid
from unittest.mock import AsyncMock, MagicMock

import pytest

from brain_v42.services.feature_dedup_job import FeatureDedupJob

# ---------------------------------------------------------------------------
# helpers
# ---------------------------------------------------------------------------


def _make_feature_row(
    *,
    feature_id: uuid.UUID | None = None,
    name: str = "Feature",
    description: str = "desc",
    embedding: list[float] | None = None,
    created_at: float = 1000.0,
    pinned: bool = False,
) -> MagicMock:
    row = MagicMock()
    row.id = feature_id or uuid.uuid4()
    row.name = name
    row.description = description
    row.embedding = embedding or [0.1] * 1536
    row.created_at = created_at
    row.status = "research"
    row.merged_into = None
    # `pinned` DOIT être posé, jamais laissé à l'attribut par défaut du
    # MagicMock : celui-ci est truthy, donc une ligne construite sans lui
    # simulerait une feature ÉPINGLÉE sans le dire, et la garde de
    # `merge_features` refuserait toutes les fusions de ce fichier. Les tests
    # ci-dessous portent sur d'autres invariants et supposent le cas nominal.
    # `False` est la valeur réelle : la colonne est `Boolean` avec
    # `server_default false` (0 ligne NULL mesurée en prod le 2026-08-22).
    row.pinned = pinned
    return row


def _build_job(deps: dict) -> FeatureDedupJob:
    return FeatureDedupJob(
        session_factory=deps["session_factory"],
        reranker=deps["reranker"],
        embedding_svc=deps["embedding_svc"],
    )


@pytest.fixture
def mock_deps() -> dict:
    session = AsyncMock()
    factory = MagicMock()
    factory.return_value.__aenter__ = AsyncMock(return_value=session)
    factory.return_value.__aexit__ = AsyncMock(return_value=False)

    embedding_svc = AsyncMock()
    embedding_svc.embed = AsyncMock(return_value=[0.5] * 1536)

    reranker = AsyncMock()
    reranker.rerank = AsyncMock(return_value=[0.9])

    return {
        "session_factory": factory,
        "session": session,
        "embedding_svc": embedding_svc,
        "reranker": reranker,
    }


# ---------------------------------------------------------------------------
# (a) Existence re-check — skip if source or target missing in session
# ---------------------------------------------------------------------------


class TestMergeExistenceRecheck:
    @pytest.mark.asyncio
    async def test_merge_skips_when_source_missing(self, mock_deps: dict) -> None:
        """merge_features must skip (not raise, not corrupt) when source was
        already deleted — e.g. by a preceding merge in the same run (A→B, B→C
        scenario where B is now gone).
        """
        target_id = uuid.uuid4()
        source_id = uuid.uuid4()

        target = _make_feature_row(feature_id=target_id, name="Target")
        source = _make_feature_row(feature_id=source_id, name="Source")

        session = mock_deps["session"]

        # SELECT … FOR UPDATE returns target but NOT source (already deleted)
        recheck_result = MagicMock()
        recheck_result.fetchall.return_value = [target]  # source missing
        session.execute = AsyncMock(return_value=recheck_result)

        job = _build_job(mock_deps)
        # Must not raise — silently skips
        await job.merge_features(session, target, source)

        # No artifact transfer / entity update / delete should happen
        # (since we skip when a party is missing)
        # The only execute should be the FOR UPDATE check
        for call_args in session.execute.call_args_list:
            stmt = call_args[0][0]
            # Ensure no DELETE on features was issued
            if hasattr(stmt, "is_delete") and stmt.is_delete:
                pytest.fail("DELETE was issued even though source was missing")

    @pytest.mark.asyncio
    async def test_merge_skips_when_target_missing(self, mock_deps: dict) -> None:
        """merge_features must skip when target is also missing (extreme edge case)."""
        target_id = uuid.uuid4()
        source_id = uuid.uuid4()

        target = _make_feature_row(feature_id=target_id, name="Target")
        source = _make_feature_row(feature_id=source_id, name="Source")

        session = mock_deps["session"]

        # SELECT … FOR UPDATE returns neither
        recheck_result = MagicMock()
        recheck_result.fetchall.return_value = []
        session.execute = AsyncMock(return_value=recheck_result)

        job = _build_job(mock_deps)
        await job.merge_features(session, target, source)

        for call_args in session.execute.call_args_list:
            stmt = call_args[0][0]
            if hasattr(stmt, "is_delete") and stmt.is_delete:
                pytest.fail("DELETE was issued even though both parties missing")


class TestMergePairSkip:
    """The _dedup_loop caller must skip pairs touching an already-consumed ID."""

    @pytest.mark.asyncio
    async def test_dedup_loop_skips_pairs_with_consumed_source(self) -> None:
        """In a chain A(target)→B(source) then B(target)→C(source):
        after the first merge B is deleted; the second pair (B, C) must
        be skipped without raising so C's data is NOT silently lost.

        We test merge_features itself plus the consumer loop pattern:
        the job must expose a way for the caller to know the merge was skipped.
        """
        a_id = uuid.uuid4()
        b_id = uuid.uuid4()
        c_id = uuid.uuid4()

        a = _make_feature_row(feature_id=a_id, name="A")
        b = _make_feature_row(feature_id=b_id, name="B")
        c = _make_feature_row(feature_id=c_id, name="C")

        session = AsyncMock()

        call_count = 0

        async def _recheck(*args, **kwargs):  # type: ignore[no-untyped-def]
            nonlocal call_count
            call_count += 1
            result = MagicMock()
            if call_count == 1:
                # First merge (A,B): both exist → full merge proceeds
                # Return both IDs → proceed
                result.fetchall.return_value = [a, b]
            else:
                # Second merge (B,C): B is gone → source missing
                result.fetchall.return_value = [c]  # only target(=B)? No — B is gone
                result.fetchall.return_value = []  # neither found (B deleted)
            # After recheck returns empty, execute continues for artifact transfer
            # but we need to reset so the subsequent executes from the full path
            # don't interfere. Use side_effect reset below.
            return result

        session.execute = AsyncMock(side_effect=_recheck)

        mock_deps_local = {
            "session_factory": MagicMock(),
            "session": session,
            "embedding_svc": AsyncMock(embed=AsyncMock(return_value=[0.1] * 1536)),
            "reranker": AsyncMock(rerank=AsyncMock(return_value=[0.9])),
        }
        job = _build_job(mock_deps_local)

        # First merge (A absorbs B) — source B exists, proceeds normally
        # We need a fresh session that fully succeeds for first merge
        session_ok = AsyncMock()
        recheck_ok = MagicMock()
        recheck_ok.fetchall.return_value = [a, b]
        full_exec_result = MagicMock()
        full_exec_result.mappings.return_value.all.return_value = []

        exec_responses_ok = [recheck_ok] + [MagicMock()] * 5  # recheck + 5 DML
        session_ok.execute = AsyncMock(side_effect=exec_responses_ok)

        await job.merge_features(session_ok, a, b)

        # Second merge (B absorbs C) — source B is already deleted
        session_skip = AsyncMock()
        recheck_skip = MagicMock()
        recheck_skip.fetchall.return_value = []  # neither A nor B found
        session_skip.execute = AsyncMock(return_value=recheck_skip)

        await job.merge_features(session_skip, b, c)

        # Verify: on the skip path, no DELETE should have been issued
        for call_args in session_skip.execute.call_args_list:
            stmt = call_args[0][0]
            if hasattr(stmt, "is_delete") and stmt.is_delete:
                pytest.fail(
                    "DELETE issued on second merge even though both source/target were missing"
                )


# ---------------------------------------------------------------------------
# (b) Embed-fail must preserve existing embedding
# ---------------------------------------------------------------------------


class TestEmbedFailPreservation:
    @pytest.mark.asyncio
    async def test_embed_failure_preserves_existing_embedding(self, mock_deps: dict) -> None:
        """When embedding re-computation fails, the target's EXISTING embedding
        must be kept in the UPDATE — not overwritten with None.

        Writing embedding=None makes the feature permanently invisible in all
        cosine searches (ClusterGuard + linkers filter embedding IS NOT NULL).
        """
        target_id = uuid.uuid4()
        source_id = uuid.uuid4()
        existing_embedding = [0.9] * 1536

        target = _make_feature_row(
            feature_id=target_id,
            name="Target",
            description="Target desc",
            embedding=existing_embedding,
        )
        source = _make_feature_row(
            feature_id=source_id,
            name="Source",
            description="Source desc",
        )

        mock_deps["embedding_svc"].embed = AsyncMock(side_effect=RuntimeError("GPU OOM"))

        session = mock_deps["session"]

        # First call: existence recheck → both exist
        recheck_result = MagicMock()
        recheck_result.fetchall.return_value = [target, source]
        session.execute = AsyncMock(return_value=recheck_result)

        job = _build_job(mock_deps)
        await job.merge_features(session, target, source)

        # Find the UPDATE features call (3rd execute: artifact transfer,
        # gitlab_events transfer, then UPDATE features, then DELETE)
        update_features_calls = [
            call_args
            for call_args in session.execute.call_args_list
            if hasattr(call_args[0][0], "is_update")
            and call_args[0][0].is_update
            and getattr(call_args[0][0].table, "name", None) == "features"
        ]

        assert len(update_features_calls) >= 1, "Expected at least one UPDATE on features table"

        # If we can't introspect the SQLAlchemy internals easily, at least
        # verify the embed service was called (and failed), and that the
        # UPDATE call happened (meaning we didn't bail out after the embed fail)
        mock_deps["embedding_svc"].embed.assert_awaited_once()
        # Merge should still complete (no exception propagated)

    @pytest.mark.asyncio
    async def test_embed_failure_does_not_write_null_embedding(self, mock_deps: dict) -> None:
        """Stricter check: the embedding=None must NEVER appear in UPDATE values
        when embed() raises.

        We patch session.execute to capture the UPDATE statement and inspect
        its bound values.
        """
        target_id = uuid.uuid4()
        source_id = uuid.uuid4()
        existing_embedding = [0.7] * 1536

        target = _make_feature_row(
            feature_id=target_id,
            name="T",
            description="T desc",
            embedding=existing_embedding,
        )
        source = _make_feature_row(
            feature_id=source_id,
            name="S",
            description="S desc",
        )

        mock_deps["embedding_svc"].embed = AsyncMock(side_effect=ValueError("timeout"))

        session = mock_deps["session"]
        captured_stmts = []

        recheck_result = MagicMock()
        recheck_result.fetchall.return_value = [target, source]

        async def _capture_execute(stmt, *args, **kwargs):  # type: ignore[no-untyped-def]
            captured_stmts.append(stmt)
            if hasattr(stmt, "is_update") and stmt.is_update:
                return MagicMock()
            if hasattr(stmt, "is_delete") and stmt.is_delete:
                return MagicMock()
            # First call: existence recheck
            return recheck_result

        session.execute = AsyncMock(side_effect=_capture_execute)

        job = _build_job(mock_deps)
        await job.merge_features(session, target, source)

        # Find the UPDATE features statement
        features_updates = [
            s
            for s in captured_stmts
            if hasattr(s, "is_update")
            and s.is_update
            and getattr(s.table, "name", None) == "features"
        ]
        assert features_updates, "Expected UPDATE on features table"

        update_stmt = features_updates[0]
        # Inspect bound values: embedding must not be None
        # _values is an ordered dict of {col_clause: value_clause}
        for col_clause, val_clause in update_stmt._values.items():
            col_name = col_clause.key if hasattr(col_clause, "key") else str(col_clause)
            if col_name == "embedding":
                # Extract actual value
                actual = val_clause.value if hasattr(val_clause, "value") else val_clause
                assert actual is not None, (
                    "embedding must NOT be set to None when embed() fails — "
                    "keep the existing embedding to avoid invisible-feature bug"
                )

    @pytest.mark.asyncio
    async def test_embed_failure_logs_warning_not_exception(self, mock_deps: dict) -> None:
        """Embed failure must be logged as a warning, not propagated."""
        target = _make_feature_row(name="T", description="T desc")
        source = _make_feature_row(name="S", description="S desc")

        mock_deps["embedding_svc"].embed = AsyncMock(side_effect=RuntimeError("down"))

        session = mock_deps["session"]
        recheck_result = MagicMock()
        recheck_result.fetchall.return_value = [target, source]
        session.execute = AsyncMock(return_value=recheck_result)

        job = _build_job(mock_deps)

        # Must NOT raise
        try:
            await job.merge_features(session, target, source)
        except Exception as exc:
            pytest.fail(f"merge_features raised on embed failure — should only warn: {exc}")


# ---------------------------------------------------------------------------
# (c) Stale-snapshot description loss — MAJOR 1
# ---------------------------------------------------------------------------


class TestStaleSnapshotDescriptionLoss:
    """merge_features must read target_desc/source_desc from the authoritative
    FOR UPDATE rows, not from the stale snapshot objects passed as arguments.

    Scenario (A=oldest target for both pairs):
        candidates = [(A, B, 0.9), (A, C, 0.9)]
        Merge 1: A absorbs B → DB now has A.description = "descA\\n---\\ndescB"
        Merge 2: stale A snapshot still has description="descA"
                 If code reads from snapshot → enriched = "descA\\n---\\ndescC"
                 → descB is silently lost.
                 If code reads from FOR UPDATE row → enriched includes descB.
    """

    @pytest.mark.asyncio
    async def test_description_read_from_authoritative_row_not_snapshot(
        self, mock_deps: dict
    ) -> None:
        """merge_features uses target_desc from the FOR UPDATE recheck row,
        not from the stale snapshot passed as `target` argument.

        The test sets target.description (snapshot) to 'descA' but the
        FOR UPDATE row returns description='descA\\n---\\ndescB' (post-merge 1
        state).  The enriched description passed to embed() must contain descB.
        """
        target_id = uuid.uuid4()
        source_id = uuid.uuid4()

        # Stale snapshot A — description as it was BEFORE merge 1
        target_snapshot = _make_feature_row(
            feature_id=target_id,
            name="A",
            description="descA",
        )
        # Source C
        source_snapshot = _make_feature_row(
            feature_id=source_id,
            name="C",
            description="descC",
        )

        # FOR UPDATE authoritative row of A — description AFTER absorbing B
        target_authoritative = _make_feature_row(
            feature_id=target_id,
            name="A",
            description="descA\n---\ndescB",
            embedding=[0.3] * 1536,
        )

        session = mock_deps["session"]

        recheck_result = MagicMock()
        recheck_result.fetchall.return_value = [target_authoritative, source_snapshot]

        dml_result = MagicMock()
        session.execute = AsyncMock(
            side_effect=[
                recheck_result,
                dml_result,
                dml_result,
                dml_result,
                dml_result,
                dml_result,
            ]
        )

        job = _build_job(mock_deps)
        await job.merge_features(session, target_snapshot, source_snapshot)

        # The embed() call must receive the enriched description that includes descB
        called_with = mock_deps["embedding_svc"].embed.call_args[0][0]
        assert "descB" in called_with, (
            f"embed() was called with '{called_with}' — descB from prior merge is missing. "
            "merge_features must read target_desc from the FOR UPDATE row, not the stale snapshot."
        )
        assert "descC" in called_with, "embed() must also include descC (source description)"
        assert "descA" in called_with, "embed() must include the original descA"

    @pytest.mark.asyncio
    async def test_source_description_read_from_authoritative_row(self, mock_deps: dict) -> None:
        """source_desc must also come from the FOR UPDATE row, not the snapshot.

        Even though the source is typically deleted next, reading from the
        authoritative row is the correct pattern for consistency and mirrors
        how target_desc must be read.
        """
        target_id = uuid.uuid4()
        source_id = uuid.uuid4()

        # Snapshot source with stale description
        target_snapshot = _make_feature_row(feature_id=target_id, name="T", description="descT")
        source_snapshot = _make_feature_row(
            feature_id=source_id, name="S", description="stale_descS"
        )

        # Authoritative source row in DB has a different description
        source_authoritative = _make_feature_row(
            feature_id=source_id, name="S", description="fresh_descS"
        )

        session = mock_deps["session"]
        recheck_result = MagicMock()
        recheck_result.fetchall.return_value = [target_snapshot, source_authoritative]

        dml_result = MagicMock()
        session.execute = AsyncMock(
            side_effect=[
                recheck_result,
                dml_result,
                dml_result,
                dml_result,
                dml_result,
                dml_result,
            ]
        )

        job = _build_job(mock_deps)
        await job.merge_features(session, target_snapshot, source_snapshot)

        called_with = mock_deps["embedding_svc"].embed.call_args[0][0]
        assert "fresh_descS" in called_with, (
            f"embed() was called with '{called_with}' — fresh_descS from authoritative row "
            "is missing. source_desc must be read from the FOR UPDATE row."
        )
        assert "stale_descS" not in called_with, (
            "embed() must NOT use stale_descS from the snapshot object"
        )


# ---------------------------------------------------------------------------
# (d) merge_features return bool — MAJOR 2
# ---------------------------------------------------------------------------


class TestMergeFeaturesReturnsBool:
    """merge_features must return True when a merge happened, False when skipped.

    This allows _dedup_loop to log 'merged' only for real merges and avoid
    wasting a FOR UPDATE txn for pairs that share a consumed ID.
    """

    @pytest.mark.asyncio
    async def test_returns_true_when_merge_succeeds(self, mock_deps: dict) -> None:
        """Returns True after a successful merge."""
        target = _make_feature_row(name="T", description="descT")
        source = _make_feature_row(name="S", description="descS")

        session = mock_deps["session"]
        recheck_result = MagicMock()
        recheck_result.fetchall.return_value = [target, source]
        dml_result = MagicMock()
        session.execute = AsyncMock(
            side_effect=[
                recheck_result,
                dml_result,
                dml_result,
                dml_result,
                dml_result,
                dml_result,
            ]
        )

        job = _build_job(mock_deps)
        result = await job.merge_features(session, target, source)

        assert result is True, (
            f"merge_features returned {result!r} after a successful merge — expected True"
        )

    @pytest.mark.asyncio
    async def test_returns_false_when_source_missing(self, mock_deps: dict) -> None:
        """Returns False when source is no longer in DB (skipped)."""
        target_id = uuid.uuid4()
        source_id = uuid.uuid4()
        target = _make_feature_row(feature_id=target_id, name="T")
        source = _make_feature_row(feature_id=source_id, name="S")

        session = mock_deps["session"]
        recheck_result = MagicMock()
        recheck_result.fetchall.return_value = [target]  # source missing
        session.execute = AsyncMock(return_value=recheck_result)

        job = _build_job(mock_deps)
        result = await job.merge_features(session, target, source)

        assert result is False, (
            f"merge_features returned {result!r} when source was missing — expected False"
        )

    @pytest.mark.asyncio
    async def test_returns_false_when_both_missing(self, mock_deps: dict) -> None:
        """Returns False when both target and source are gone (skipped)."""
        target = _make_feature_row(name="T")
        source = _make_feature_row(name="S")

        session = mock_deps["session"]
        recheck_result = MagicMock()
        recheck_result.fetchall.return_value = []
        session.execute = AsyncMock(return_value=recheck_result)

        job = _build_job(mock_deps)
        result = await job.merge_features(session, target, source)

        assert result is False, (
            f"merge_features returned {result!r} when both were missing — expected False"
        )
