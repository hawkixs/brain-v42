"""Unit tests for ClusterGuard — anti-duplication resolver."""

from __future__ import annotations

import uuid
from typing import Any
from unittest.mock import AsyncMock, MagicMock

import pytest

from brain_v42.automation.ownership import OwnershipLostError
from brain_v42.services.cluster_guard import ClusterGuard

# ── fixtures ────────────────────────────────────────────────────────────


def _make_feature_row(
    *,
    name: str = "Test Feature",
    description: str = "A test feature",
    status: str = "planned",
    similarity: float = 0.80,
    pinned: bool = False,
) -> MagicMock:
    """Create a mock DB row resembling a features table row."""
    row = MagicMock()
    row.id = uuid.uuid4()
    row.name = name
    row.description = description
    row.status = status
    row.similarity = similarity
    row.pinned = pinned
    row.project_key = "brain_v42"
    return row


@pytest.fixture
def mock_deps():
    """Create mock dependencies for ClusterGuard."""
    session = AsyncMock()
    factory = MagicMock()
    factory.return_value.__aenter__ = AsyncMock(return_value=session)
    factory.return_value.__aexit__ = AsyncMock(return_value=False)

    embedding_svc = AsyncMock()
    embedding_svc.embed = AsyncMock(return_value=[0.1] * 1536)

    reranker = AsyncMock()
    reranker.is_available = AsyncMock(return_value=True)
    reranker.rerank = AsyncMock(return_value=[0.80])

    status_engine = MagicMock()
    status_engine.compute_status = MagicMock(return_value="research")

    return {
        "session_factory": factory,
        "session": session,
        "embedding_svc": embedding_svc,
        "reranker": reranker,
        "status_engine": status_engine,
    }


def _build_guard(deps: dict[str, Any]) -> ClusterGuard:
    return ClusterGuard(
        session_factory=deps["session_factory"],
        embedding_svc=deps["embedding_svc"],
        reranker=deps["reranker"],
        status_engine=deps["status_engine"],
    )


# ── tests ───────────────────────────────────────────────────────────────


@pytest.mark.asyncio
async def test_resolve_links_when_high_cosine(mock_deps):
    """Cosine >= 0.70 -> linked, skip reranker. Status promotion is preserved
    for knowledge signals: planned -> research."""
    feature_row = _make_feature_row(similarity=0.85, status="planned")
    result_set = MagicMock()
    result_set.fetchall.return_value = [feature_row]

    # First execute: cosine search returns a high-similarity feature
    # Second execute: update status (if needed)
    mock_deps["session"].execute = AsyncMock(return_value=result_set)

    guard = _build_guard(mock_deps)
    feature, action = await guard.resolve(
        text="memory decay system",
        embedding=[0.1] * 1536,
        project_key="brain_v42",
        signal_type="learning",
    )

    assert action == "linked"
    assert feature.id == feature_row.id
    # Reranker should NOT have been called
    mock_deps["reranker"].rerank.assert_not_called()
    # Status promotion (planned -> research) is preserved on "linked".
    mock_deps["status_engine"].compute_status.assert_called_once_with("planned", "learning", False)


@pytest.mark.asyncio
async def test_resolve_creates_when_no_candidates(mock_deps):
    """No matching features, work signal (plan) -> created.

    Non-regression: creation must stay intact for CREATING_SIGNALS.
    """
    result_set = MagicMock()
    result_set.fetchall.return_value = []

    # First execute: cosine search returns nothing
    # Second execute: INSERT returning new row
    new_feature_row = _make_feature_row(name="memory decay system"[:200])
    insert_result = MagicMock()
    insert_result.fetchone.return_value = new_feature_row

    mock_deps["session"].execute = AsyncMock(side_effect=[result_set, insert_result])

    guard = _build_guard(mock_deps)
    feature, action = await guard.resolve(
        text="memory decay system",
        embedding=[0.1] * 1536,
        project_key="brain_v42",
        signal_type="plan",
    )

    assert action == "created"
    assert feature.id == new_feature_row.id


@pytest.mark.asyncio
async def test_resolve_skips_when_no_candidates_for_knowledge_signal(mock_deps):
    """Link-only mode: a knowledge signal (learning) with no candidate is
    skipped, never creates a feature."""
    result_set = MagicMock()
    result_set.fetchall.return_value = []
    mock_deps["session"].execute = AsyncMock(return_value=result_set)

    guard = _build_guard(mock_deps)
    feature, action = await guard.resolve(
        text="memory decay system",
        embedding=[0.1] * 1536,
        project_key="brain_v42",
        signal_type="learning",
    )

    assert (feature, action) == (None, "skipped")
    # Only the candidate search ran — no INSERT into features.
    assert mock_deps["session"].execute.await_count == 1


@pytest.mark.asyncio
async def test_resolve_skips_for_unknown_signal_type(mock_deps):
    """Allowlist is fail-closed: an unrecognized signal_type never creates."""
    result_set = MagicMock()
    result_set.fetchall.return_value = []
    mock_deps["session"].execute = AsyncMock(return_value=result_set)

    guard = _build_guard(mock_deps)
    feature, action = await guard.resolve(
        text="some new tool output",
        embedding=[0.1] * 1536,
        project_key="brain_v42",
        signal_type="type_inconnu",
    )

    assert (feature, action) == (None, "skipped")
    assert mock_deps["session"].execute.await_count == 1


@pytest.mark.asyncio
async def test_resolve_uses_reranker_in_grey_zone(mock_deps):
    """Cosine 0.50-0.70 -> calls reranker to decide. Reranker >= 0.75 -> linked."""
    feature_row = _make_feature_row(similarity=0.60, name="Memory Decay")
    result_set = MagicMock()
    result_set.fetchall.return_value = [feature_row]

    mock_deps["session"].execute = AsyncMock(return_value=result_set)
    # Reranker returns high score -> should link
    mock_deps["reranker"].rerank = AsyncMock(return_value=[0.82])

    guard = _build_guard(mock_deps)
    feature, action = await guard.resolve(
        text="decay system",
        embedding=[0.1] * 1536,
        project_key="brain_v42",
        signal_type="decision",
    )

    assert action == "linked"
    assert feature.id == feature_row.id
    mock_deps["reranker"].rerank.assert_awaited_once()


@pytest.mark.asyncio
async def test_resolve_falls_back_when_reranker_down(mock_deps):
    """Reranker unavailable -> cosine-only fallback.
    Cosine 0.65+ -> linked with fallback threshold.
    """
    feature_row = _make_feature_row(similarity=0.66)
    result_set = MagicMock()
    result_set.fetchall.return_value = [feature_row]

    mock_deps["session"].execute = AsyncMock(return_value=result_set)
    mock_deps["reranker"].is_available = AsyncMock(return_value=False)

    guard = _build_guard(mock_deps)
    feature, action = await guard.resolve(
        text="some signal",
        embedding=[0.1] * 1536,
        project_key="brain_v42",
        signal_type="snippet",
    )

    assert action == "linked"
    assert feature.id == feature_row.id
    # Reranker.rerank should NOT be called since it's down
    mock_deps["reranker"].rerank.assert_not_called()


@pytest.mark.asyncio
async def test_resolve_falls_back_creates_when_below_fallback(mock_deps):
    """Reranker unavailable + cosine < 0.65, work signal (push) -> created
    (skip merge zone). Non-regression: uses a CREATING_SIGNALS type."""
    feature_row = _make_feature_row(similarity=0.55)
    result_set = MagicMock()
    result_set.fetchall.return_value = [feature_row]

    new_feature_row = _make_feature_row(name="some signal"[:200])
    insert_result = MagicMock()
    insert_result.fetchone.return_value = new_feature_row

    mock_deps["session"].execute = AsyncMock(side_effect=[result_set, insert_result])
    mock_deps["reranker"].is_available = AsyncMock(return_value=False)

    guard = _build_guard(mock_deps)
    feature, action = await guard.resolve(
        text="some signal",
        embedding=[0.1] * 1536,
        project_key="brain_v42",
        signal_type="push",
    )

    assert action == "created"
    mock_deps["reranker"].rerank.assert_not_called()


@pytest.mark.asyncio
async def test_resolve_merges_in_reranker_merge_zone(mock_deps):
    """Reranker score 0.50-0.75, work signal (mr_opened) -> merged, description
    enriched. Non-regression: merge is preserved for CREATING_SIGNALS."""
    feature_row = _make_feature_row(
        similarity=0.60,
        name="Memory System",
        description="Original description",
    )
    result_set = MagicMock()
    result_set.fetchall.return_value = [feature_row]

    # Reranker returns score in merge zone (0.50-0.75)
    mock_deps["reranker"].rerank = AsyncMock(return_value=[0.62])

    # execute calls: 1) cosine search, 2) update description+embedding, 3) update status
    mock_deps["session"].execute = AsyncMock(return_value=result_set)

    guard = _build_guard(mock_deps)
    feature, action = await guard.resolve(
        text="decay memory management",
        embedding=[0.1] * 1536,
        project_key="brain_v42",
        signal_type="mr_opened",
    )

    assert action == "merged"
    assert feature.id == feature_row.id
    # Embedding service should have been called to re-embed enriched description
    mock_deps["embedding_svc"].embed.assert_awaited()


@pytest.mark.asyncio
async def test_resolve_skips_in_grey_zone_for_knowledge_signal(mock_deps):
    """Reranker score 0.60 (merge zone), knowledge signal (learning) -> skipped.

    Proves the removal of `merged` for knowledge: the candidate feature's
    description must stay untouched and no re-embedding happens.
    """
    feature_row = _make_feature_row(
        similarity=0.60,
        name="Memory System",
        description="Original description",
    )
    result_set = MagicMock()
    result_set.fetchall.return_value = [feature_row]

    mock_deps["reranker"].rerank = AsyncMock(return_value=[0.60])
    mock_deps["session"].execute = AsyncMock(return_value=result_set)

    guard = _build_guard(mock_deps)
    feature, action = await guard.resolve(
        text="decay memory management",
        embedding=[0.1] * 1536,
        project_key="brain_v42",
        signal_type="learning",
    )

    assert (feature, action) == (None, "skipped")
    assert feature_row.description == "Original description"
    mock_deps["embedding_svc"].embed.assert_not_awaited()
    # Only the candidate search ran — no UPDATE.
    assert mock_deps["session"].execute.await_count == 1


@pytest.mark.asyncio
async def test_resolve_creates_when_reranker_score_low(mock_deps):
    """Reranker score < 0.50, work signal (mr_opened) -> created (not similar
    enough). Non-regression: uses a CREATING_SIGNALS type."""
    feature_row = _make_feature_row(similarity=0.55)
    result_set = MagicMock()
    result_set.fetchall.return_value = [feature_row]

    new_feature_row = _make_feature_row(name="unrelated signal")
    insert_result = MagicMock()
    insert_result.fetchone.return_value = new_feature_row

    # Reranker returns low score
    mock_deps["reranker"].rerank = AsyncMock(return_value=[0.30])

    mock_deps["session"].execute = AsyncMock(side_effect=[result_set, insert_result])

    guard = _build_guard(mock_deps)
    feature, action = await guard.resolve(
        text="unrelated signal",
        embedding=[0.1] * 1536,
        project_key="brain_v42",
        signal_type="mr_opened",
    )

    assert action == "created"
    mock_deps["reranker"].rerank.assert_awaited_once()


@pytest.mark.asyncio
async def test_resolve_creates_when_cosine_below_grey(mock_deps):
    """Cosine < 0.50, work signal (plan) -> created immediately, skip
    reranker. Non-regression: uses a CREATING_SIGNALS type."""
    feature_row = _make_feature_row(similarity=0.35)
    result_set = MagicMock()
    result_set.fetchall.return_value = [feature_row]

    new_feature_row = _make_feature_row(name="totally different")
    insert_result = MagicMock()
    insert_result.fetchone.return_value = new_feature_row

    mock_deps["session"].execute = AsyncMock(side_effect=[result_set, insert_result])

    guard = _build_guard(mock_deps)
    feature, action = await guard.resolve(
        text="totally different",
        embedding=[0.1] * 1536,
        project_key="brain_v42",
        signal_type="plan",
    )

    assert action == "created"
    mock_deps["reranker"].rerank.assert_not_called()


# ── reranker failure graceful degradation ──────────────────────────────


@pytest.mark.asyncio
async def test_resolve_falls_back_to_cosine_when_rerank_raises(mock_deps):
    """When reranker.rerank() raises, fall back to cosine similarity scores."""
    feature_row = _make_feature_row(similarity=0.60, name="Memory Decay")
    result_set = MagicMock()
    result_set.fetchall.return_value = [feature_row]

    mock_deps["session"].execute = AsyncMock(return_value=result_set)
    # Reranker is available but rerank() itself raises
    mock_deps["reranker"].is_available = AsyncMock(return_value=True)
    mock_deps["reranker"].rerank = AsyncMock(side_effect=Exception("reranker crashed"))

    guard = _build_guard(mock_deps)
    # Should NOT raise — degrades gracefully using cosine scores
    feature, action = await guard.resolve(
        text="decay system",
        embedding=[0.1] * 1536,
        project_key="brain_v42",
        signal_type="decision",
    )

    # rerank() raised, so scores fall back to the cosine similarities: [0.60].
    # 0.60 lands in the merge zone (>= RERANKER_MERGE 0.50, < RERANKER_LINK
    # 0.75), and "decision" is outside CREATING_SIGNALS — link-only mode turns
    # that merge into a skip. Deterministic: assert the exact outcome rather
    # than a set of every possible action, which would prove nothing.
    assert action == "skipped"
    assert feature is None


# ── feature.name sanitization on create ─────────────────────────────────


def _bound_name_from_insert(execute_mock) -> str:
    """Extract the `name` bound param from the second execute call (INSERT)."""
    insert_stmt = execute_mock.call_args_list[1].args[0]
    return insert_stmt.compile().params["name"]


@pytest.mark.asyncio
async def test_create_feature_uses_first_line_when_text_is_multiline(mock_deps):
    """Multi-paragraph artifact text → name is the first non-empty line only.

    Without this, names like `feat(x): title\\n\\nlong description ...` leak
    multi-line content into the briefing.
    """
    multiline = (
        "docs(plan): mark Dream v3 Spec A code complete\n"
        "\n"
        "Plan file had 65 unchecked items despite the code\n"
        "being merged on main between Apr 19-20."
    )
    result_set = MagicMock()
    result_set.fetchall.return_value = []
    new_row = _make_feature_row()
    insert_result = MagicMock()
    insert_result.fetchone.return_value = new_row
    mock_deps["session"].execute = AsyncMock(side_effect=[result_set, insert_result])

    guard = _build_guard(mock_deps)
    await guard.resolve(
        text=multiline,
        embedding=[0.1] * 1536,
        project_key="brain_v42",
        signal_type="plan",
    )

    name = _bound_name_from_insert(mock_deps["session"].execute)
    assert "\n" not in name
    assert name == "docs(plan): mark Dream v3 Spec A code complete"


@pytest.mark.asyncio
async def test_create_feature_skips_leading_blank_lines(mock_deps):
    """Leading blank lines/whitespace → first *non-empty* line wins."""
    text = "\n\n   \nreal title here\nbody"
    result_set = MagicMock()
    result_set.fetchall.return_value = []
    insert_result = MagicMock()
    insert_result.fetchone.return_value = _make_feature_row()
    mock_deps["session"].execute = AsyncMock(side_effect=[result_set, insert_result])

    guard = _build_guard(mock_deps)
    await guard.resolve(
        text=text,
        embedding=[0.1] * 1536,
        project_key="brain_v42",
        signal_type="plan",
    )

    assert _bound_name_from_insert(mock_deps["session"].execute) == "real title here"


@pytest.mark.asyncio
async def test_create_feature_truncates_long_single_line(mock_deps):
    """Single-line text longer than 200 chars is still truncated."""
    long_line = "a" * 500
    result_set = MagicMock()
    result_set.fetchall.return_value = []
    insert_result = MagicMock()
    insert_result.fetchone.return_value = _make_feature_row()
    mock_deps["session"].execute = AsyncMock(side_effect=[result_set, insert_result])

    guard = _build_guard(mock_deps)
    await guard.resolve(
        text=long_line,
        embedding=[0.1] * 1536,
        project_key="brain_v42",
        signal_type="plan",
    )

    name = _bound_name_from_insert(mock_deps["session"].execute)
    assert len(name) == 200
    assert name == "a" * 200


@pytest.mark.asyncio
async def test_create_feature_uses_untitled_fallback_for_whitespace_only_text(mock_deps):
    """All-whitespace text → fall back to '(untitled)' (NOT NULL column)."""
    text = "   \n\n\t  \n"
    result_set = MagicMock()
    result_set.fetchall.return_value = []
    insert_result = MagicMock()
    insert_result.fetchone.return_value = _make_feature_row()
    mock_deps["session"].execute = AsyncMock(side_effect=[result_set, insert_result])

    guard = _build_guard(mock_deps)
    await guard.resolve(
        text=text,
        embedding=[0.1] * 1536,
        project_key="brain_v42",
        signal_type="plan",
    )

    assert _bound_name_from_insert(mock_deps["session"].execute) == "(untitled)"


# ── archived / merged_into filter in _find_candidates ────────────────────


@pytest.mark.asyncio
async def test_find_candidates_excludes_archived_and_merged(mock_deps):
    """_find_candidates SQL must filter out archived and merged-into features.

    An archived cluster must never be returned as a candidate — otherwise
    ClusterGuard.resolve() would call _maybe_update_status on it, which in turn
    calls StatusEngine.compute_status('archived', ...) → ValueError pre-fix.
    """
    result_set = MagicMock()
    result_set.fetchall.return_value = []
    mock_deps["session"].execute = AsyncMock(return_value=result_set)

    guard = _build_guard(mock_deps)
    await guard.resolve(
        text="some signal",
        embedding=[0.1] * 1536,
        project_key="brain_v42",
        signal_type="learning",
    )

    # The first execute call is _find_candidates; inspect the compiled statement.
    first_stmt = mock_deps["session"].execute.call_args_list[0].args[0]
    # Compile with literal_binds=True so param values (like 'archived') appear in SQL.
    compiled_sql = str(first_stmt.compile(compile_kwargs={"literal_binds": True}))
    assert "archived" in compiled_sql
    assert "merged_into" in compiled_sql


class _MutableMutationGate:
    """Deterministic ownership probe used at async mutation boundaries."""

    def __init__(self) -> None:
        self.owned = True

    def ensure_owned(self) -> None:
        if not self.owned:
            raise OwnershipLostError("ownership lost inside cluster resolution")


@pytest.mark.asyncio
async def test_resolve_stops_after_ownership_loss_during_candidate_query(mock_deps):
    """Candidate I/O may not be followed by feature creation after lease loss."""
    gate = _MutableMutationGate()
    no_candidates = MagicMock()
    no_candidates.fetchall.return_value = []
    inserted = MagicMock()
    inserted.fetchone.return_value = _make_feature_row()
    results = iter((no_candidates, inserted))
    execute_count = 0

    async def execute_and_lose_during_candidates(_statement):
        nonlocal execute_count
        execute_count += 1
        result = next(results)
        if execute_count == 1:
            gate.owned = False
        return result

    mock_deps["session"].execute = AsyncMock(side_effect=execute_and_lose_during_candidates)
    guard = _build_guard(mock_deps)
    guard._mutation_guard = gate.ensure_owned  # type: ignore[attr-defined]

    with pytest.raises(OwnershipLostError, match="inside cluster resolution"):
        await guard.resolve(
            text="new webhook signal",
            embedding=[0.1] * 1536,
            project_key="brain_v42",
            signal_type="mr_opened",
        )

    assert execute_count == 1
    mock_deps["session"].commit.assert_not_awaited()


@pytest.mark.asyncio
async def test_resolve_stops_after_ownership_loss_during_reranking(mock_deps):
    """Reranker I/O may not be followed by status mutation after lease loss."""
    gate = _MutableMutationGate()
    candidate = _make_feature_row(similarity=0.60, status="planned")
    candidates = MagicMock()
    candidates.fetchall.return_value = [candidate]
    mock_deps["session"].execute = AsyncMock(return_value=candidates)

    async def rerank_after_losing_ownership(
        _text: str,
        _candidate_texts: list[str],
    ) -> list[float]:
        gate.owned = False
        return [0.82]

    mock_deps["reranker"].rerank = AsyncMock(side_effect=rerank_after_losing_ownership)
    guard = _build_guard(mock_deps)
    guard._mutation_guard = gate.ensure_owned  # type: ignore[attr-defined]

    with pytest.raises(OwnershipLostError, match="inside cluster resolution"):
        await guard.resolve(
            text="grey-zone webhook signal",
            embedding=[0.1] * 1536,
            project_key="brain_v42",
            signal_type="mr_opened",
        )

    assert mock_deps["session"].execute.await_count == 1
    mock_deps["session"].commit.assert_not_awaited()


@pytest.mark.asyncio
async def test_resolve_does_not_commit_when_ownership_is_lost_during_insert(mock_deps):
    """A post-INSERT lease check must prevent a newly created feature commit."""
    gate = _MutableMutationGate()
    no_candidates = MagicMock()
    no_candidates.fetchall.return_value = []
    inserted = MagicMock()
    inserted.fetchone.return_value = _make_feature_row()
    results = iter((no_candidates, inserted))
    execute_count = 0

    async def execute_and_lose_during_insert(_statement):
        nonlocal execute_count
        execute_count += 1
        result = next(results)
        if execute_count == 2:
            gate.owned = False
        return result

    mock_deps["session"].execute = AsyncMock(side_effect=execute_and_lose_during_insert)
    guard = _build_guard(mock_deps)
    guard._mutation_guard = gate.ensure_owned  # type: ignore[attr-defined]

    with pytest.raises(OwnershipLostError, match="inside cluster resolution"):
        await guard.resolve(
            text="new webhook signal",
            embedding=[0.1] * 1536,
            project_key="brain_v42",
            signal_type="mr_opened",
        )

    assert execute_count == 2
    mock_deps["session"].commit.assert_not_awaited()


@pytest.mark.asyncio
async def test_merge_stops_after_ownership_loss_during_reembedding(mock_deps):
    """Re-embedding may not be followed by UPDATE or commit after lease loss."""
    gate = _MutableMutationGate()
    candidate = _make_feature_row(
        similarity=0.60,
        description="existing feature description",
    )
    candidates = MagicMock()
    candidates.fetchall.return_value = [candidate]
    mock_deps["session"].execute = AsyncMock(return_value=candidates)
    mock_deps["reranker"].rerank = AsyncMock(return_value=[0.62])

    async def reembed_after_losing_ownership(_text: str) -> list[float]:
        gate.owned = False
        return [0.2] * 1536

    mock_deps["embedding_svc"].embed = AsyncMock(side_effect=reembed_after_losing_ownership)
    guard = _build_guard(mock_deps)
    guard._mutation_guard = gate.ensure_owned  # type: ignore[attr-defined]

    with pytest.raises(OwnershipLostError, match="inside cluster resolution"):
        await guard.resolve(
            text="description extension from webhook",
            embedding=[0.1] * 1536,
            project_key="brain_v42",
            signal_type="mr_opened",
        )

    assert mock_deps["session"].execute.await_count == 1
    mock_deps["session"].commit.assert_not_awaited()


class TestCreatingSignalsAllowlistIsGuarded:
    """The allowlist that dried up the roadmap pollution had no witness.

    Measured on 2026-08-11, fifteen days either side of the link-only switch of
    2026-08-03: **9.27 features created per day before, 0.13 after** — a 71×
    reduction. The only later creation comes from an indexed PLAN, hence from an
    explicitly authorised signal, with its three `plan`-typed artifacts. Zero
    creations from a knowledge artifact in eight days.

    That drying-up rests entirely on `CREATING_SIGNALS`, and the CONTENT of that
    frozenset was asserted nowhere: the neighbouring tests use it ("uses a
    CREATING_SIGNALS type") without ever saying what must or must not be in it.
    Putting `learning` back would break no test, and the 73 `research`
    pseudo-features — learnings and commit messages promoted into features — would
    start piling up again in silence.
    """

    KNOWLEDGE_SIGNALS = ("learning", "decision", "snippet", "runbook", "adr")

    def test_no_knowledge_artifact_may_create_a_feature(self) -> None:
        from brain_v42.services.cluster_guard import CREATING_SIGNALS

        admitted = set(self.KNOWLEDGE_SIGNALS) & CREATING_SIGNALS

        assert not admitted, (
            "un type d'artefact de connaissance peut de nouveau créer une feature : "
            f"{sorted(admitted)} — c'est le robinet que le mode link-only a fermé"
        )

    def test_the_signals_that_must_still_create_are_named(self) -> None:
        """Exhaustive positive control.

        Without it, emptying the list entirely would make the absence assertion
        true for nothing — and would break plan indexing and GitLab ingestion along
        the way, without any test saying so.
        """
        from brain_v42.services.cluster_guard import CREATING_SIGNALS

        assert CREATING_SIGNALS == frozenset(
            {
                "plan",
                "mr_opened",
                "mr_merged",
                "push",
                "pipeline_success",
                "pipeline_failure",
            }
        )

    def test_the_allowlist_is_immutable(self) -> None:
        """`frozenset` and not `set`: an allowlist mutable at runtime would be
        modifiable by any caller, and the drying-up would depend on the import
        order."""
        from brain_v42.services.cluster_guard import CREATING_SIGNALS

        assert isinstance(CREATING_SIGNALS, frozenset)
