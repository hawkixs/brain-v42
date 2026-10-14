"""Unit tests for ClusterGuard — anti-duplication resolver."""

from __future__ import annotations

import uuid
from typing import Any
from unittest.mock import AsyncMock, MagicMock

import pytest
import sqlalchemy as sa
from structlog.testing import capture_logs

from brain_v42.automation.ownership import OwnershipLostError
from brain_v42.services.cluster_guard import CREATING_SIGNALS, FALLBACK_LINK, ClusterGuard

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

    status_engine = MagicMock()
    status_engine.compute_status = MagicMock(return_value="research")

    return {
        "session_factory": factory,
        "session": session,
        "embedding_svc": embedding_svc,
        "status_engine": status_engine,
    }


def _build_guard(deps: dict[str, Any]) -> ClusterGuard:
    return ClusterGuard(
        session_factory=deps["session_factory"],
        embedding_svc=deps["embedding_svc"],
        status_engine=deps["status_engine"],
    )


# ── tests ───────────────────────────────────────────────────────────────


@pytest.mark.asyncio
async def test_status_update_compares_the_live_candidate_status(mock_deps: dict[str, Any]) -> None:
    """A stale candidate must not overwrite a curation decision after waiting."""
    feature = _make_feature_row(status="planned")
    result = MagicMock(rowcount=1)
    mock_deps["session"].execute = AsyncMock(return_value=result)

    await _build_guard(mock_deps)._maybe_update_status(mock_deps["session"], feature, "learning")

    statement = mock_deps["session"].execute.call_args.args[0]
    sql = str(statement.compile(compile_kwargs={"literal_binds": True}))
    assert "features.merged_into IS NULL" in sql
    assert "features.status != 'archived'" in sql
    assert "features.status = 'planned'" in sql


@pytest.mark.asyncio
@pytest.mark.parametrize("similarity", [0.85, 0.66])
@pytest.mark.parametrize("lost_state", ["archived", "merged", "gone"])
async def test_resolve_reports_lost_when_status_compare_and_swap_fails(
    mock_deps: dict[str, Any], similarity: float, lost_state: str
) -> None:
    """Both linking branches must refuse a candidate changed by curation."""
    candidate = _make_feature_row(similarity=similarity)
    search_result = MagicMock()
    search_result.fetchall.return_value = [candidate]
    fresh = _make_feature_row(status="archived" if lost_state == "archived" else "planned")
    fresh.merged_into = uuid.uuid4() if lost_state == "merged" else None
    fresh_result = MagicMock()
    fresh_result.fetchone.return_value = None if lost_state == "gone" else fresh

    async def execute(statement: Any, *args: Any) -> MagicMock:
        if isinstance(statement, sa.sql.dml.Update):
            return MagicMock(rowcount=0)
        if (
            isinstance(statement, sa.sql.selectable.Select)
            and len(statement._order_by_clauses) == 0
        ):
            return fresh_result
        return search_result

    mock_deps["session"].execute = AsyncMock(side_effect=execute)
    with capture_logs() as logs:
        outcome = await _build_guard(mock_deps).resolve(
            "Private signal body", [0.1] * 1536, "brain-v42", "learning"
        )

    assert outcome == (None, "lost")
    assert not any(log["event"] == "cluster_guard.status_update_skipped" for log in logs)
    assert "Private signal body" not in str(logs)
    assert not any(log["event"] == "cluster_guard.status_updated" for log in logs)
    mock_deps["session"].commit.assert_not_awaited()


@pytest.mark.asyncio
@pytest.mark.parametrize("second_wins", [True, False])
async def test_status_cas_retries_from_fresh_live_status(mock_deps, second_wins):
    """A status-only race preserves the link and never regresses the fresh status."""
    candidate = _make_feature_row(status="planned")
    fresh = _make_feature_row(status="building", pinned=True)
    fresh.merged_into = None
    fresh_result = MagicMock()
    fresh_result.fetchone.return_value = fresh
    mock_deps["session"].execute.side_effect = [
        MagicMock(rowcount=0),
        fresh_result,
        MagicMock(rowcount=int(second_wins)),
    ]
    mock_deps["status_engine"].compute_status.side_effect = ["research", "done"]

    with capture_logs() as logs:
        assert (
            await _build_guard(mock_deps)._maybe_update_status(
                mock_deps["session"], candidate, "learning"
            )
            is True
        )

    assert mock_deps["status_engine"].compute_status.call_args_list[-1].args == (
        "building",
        "learning",
        True,
    )
    retry = mock_deps["session"].execute.call_args.args[0]
    assert retry.compile().params["status_2"] == "building"
    assert retry.compile().params["status"] == "done"
    assert any(log["event"] == "cluster_guard.status_update_skipped" for log in logs) is (
        not second_wins
    )


@pytest.mark.asyncio
async def test_status_cas_keeps_a_live_feature_when_fresh_status_needs_no_promotion(mock_deps):
    candidate = _make_feature_row()
    fresh = _make_feature_row(status="building")
    fresh.merged_into = None
    result = MagicMock()
    result.fetchone.return_value = fresh
    mock_deps["session"].execute.side_effect = [MagicMock(rowcount=0), result]
    mock_deps["status_engine"].compute_status.side_effect = ["research", "building"]
    assert (
        await _build_guard(mock_deps)._maybe_update_status(
            mock_deps["session"], candidate, "learning"
        )
        is True
    )
    assert mock_deps["session"].execute.await_count == 2


@pytest.mark.asyncio
@pytest.mark.parametrize("first_loss", ["resolve", "insert"])
async def test_resolve_and_link_retries_a_lost_target_once(mock_deps, first_loss):
    guard = _build_guard(mock_deps)
    first, survivor = _make_feature_row(), _make_feature_row()
    guard.resolve = AsyncMock(
        side_effect=[
            (None, "lost") if first_loss == "resolve" else (first, "linked"),
            (survivor, "created"),
        ]
    )
    live, lost = MagicMock(), MagicMock()
    live.scalar_one.return_value = True
    lost.scalar_one.return_value = False
    mock_deps["session"].execute.side_effect = [live] if first_loss == "resolve" else [lost, live]
    score_for = MagicMock(return_value=0.9)
    assert await guard.resolve_and_link(
        "Signal", [0.1], "brain-v42", "plan", "plan", uuid.uuid4(), score_for
    ) == (survivor, "created", True)
    assert guard.resolve.await_count == 2
    assert score_for.call_args.args == ("created",)


@pytest.mark.asyncio
@pytest.mark.parametrize("action", ["lost", "skipped", "linked"])
async def test_resolve_and_link_bounds_losses_and_skips(mock_deps, action):
    guard = _build_guard(mock_deps)
    guard.resolve = AsyncMock(
        return_value=(_make_feature_row() if action == "linked" else None, action)
    )
    lost = MagicMock()
    lost.scalar_one.return_value = False
    mock_deps["session"].execute.return_value = lost
    outcome = await guard.resolve_and_link(
        "Signal", [0.1], "brain-v42", "learning", "learning", uuid.uuid4(), lambda _: 1.0
    )
    assert outcome == (None, "skipped" if action == "skipped" else "lost", False)
    assert guard.resolve.await_count == (1 if action == "skipped" else 2)


@pytest.mark.asyncio
async def test_resolve_locks_the_project_before_reading_candidates(
    mock_deps: dict[str, Any],
) -> None:
    """The empty-project read and subsequent creation must share one lock."""
    result = MagicMock()
    result.fetchall.return_value = []
    mock_deps["session"].execute = AsyncMock(return_value=result)

    assert await _build_guard(mock_deps).resolve(
        "Knowledge signal", [0.1] * 1536, "brain-v42", "decision"
    ) == (None, "skipped")

    calls = mock_deps["session"].execute.call_args_list
    assert "pg_advisory_xact_lock(hashtext(:key))" in str(calls[0].args[0])
    key = calls[0].args[1]["key"]
    assert key.endswith(":brain-v42")
    assert key != "brain_v42_adr_number:brain-v42"
    assert isinstance(calls[1].args[0], sa.sql.selectable.Select)
    mock_deps["session"].commit.assert_not_awaited()
    mock_deps["session_factory"].return_value.__aexit__.assert_awaited_once()


@pytest.mark.asyncio
async def test_resolve_links_when_high_cosine(mock_deps):
    """Cosine >= 0.70 -> linked. Status promotion is preserved for knowledge
    signals: planned -> research."""
    feature_row = _make_feature_row(similarity=0.85, status="planned")
    result_set = MagicMock()
    result_set.fetchall.return_value = [feature_row]

    # Advisory lock, then cosine search, then update status (if needed).
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
    # Status promotion (planned -> research) is preserved on "linked".
    mock_deps["status_engine"].compute_status.assert_called_once_with("planned", "learning", False)


@pytest.mark.asyncio
async def test_resolve_creates_when_no_candidates(mock_deps):
    """No matching features, work signal (plan) -> created.

    Non-regression: creation must stay intact for CREATING_SIGNALS.
    """
    result_set = MagicMock()
    result_set.fetchall.return_value = []

    # Advisory lock, then cosine search returns nothing, then INSERT.
    new_feature_row = _make_feature_row(name="memory decay system"[:200])
    insert_result = MagicMock()
    insert_result.fetchone.return_value = new_feature_row

    mock_deps["session"].execute = AsyncMock(side_effect=[MagicMock(), result_set, insert_result])

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
    # Only the advisory lock and candidate search ran — no INSERT.
    assert mock_deps["session"].execute.await_count == 2


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
    assert mock_deps["session"].execute.await_count == 2


@pytest.mark.asyncio
async def test_resolve_links_in_grey_zone_at_fallback_threshold(mock_deps):
    """Grey-zone cosine 0.66 >= FALLBACK_LINK 0.65 -> linked, with no reranker
    collaborator at all (ruling d4648d84: links come from cosine only)."""
    feature_row = _make_feature_row(similarity=0.66)
    result_set = MagicMock()
    result_set.fetchall.return_value = [feature_row]

    mock_deps["session"].execute = AsyncMock(return_value=result_set)

    guard = _build_guard(mock_deps)
    feature, action = await guard.resolve(
        text="some signal",
        embedding=[0.1] * 1536,
        project_key="brain_v42",
        signal_type="snippet",
    )

    assert action == "linked"
    assert feature.id == feature_row.id


@pytest.mark.asyncio
async def test_resolve_creates_below_fallback_threshold_for_creating_signal(mock_deps):
    """Grey-zone cosine 0.55 < FALLBACK_LINK, work signal (plan) -> created."""
    feature_row = _make_feature_row(similarity=0.55)
    result_set = MagicMock()
    result_set.fetchall.return_value = [feature_row]

    new_feature_row = _make_feature_row(name="some signal"[:200])
    insert_result = MagicMock()
    insert_result.fetchone.return_value = new_feature_row

    mock_deps["session"].execute = AsyncMock(side_effect=[MagicMock(), result_set, insert_result])

    guard = _build_guard(mock_deps)
    feature, action = await guard.resolve(
        text="some signal",
        embedding=[0.1] * 1536,
        project_key="brain_v42",
        signal_type="plan",
    )

    assert action == "created"
    assert feature.id == new_feature_row.id


@pytest.mark.asyncio
async def test_resolve_skips_below_fallback_threshold_for_knowledge_signal(mock_deps):
    """Grey-zone cosine 0.55, knowledge signal (learning) -> skipped.

    The candidate feature's description must stay untouched and no
    re-embedding happens.
    """
    feature_row = _make_feature_row(
        similarity=0.55,
        name="Memory System",
        description="Original description",
    )
    result_set = MagicMock()
    result_set.fetchall.return_value = [feature_row]
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
    # Only the advisory lock and candidate search ran — no UPDATE.
    assert mock_deps["session"].execute.await_count == 2


_EVERY_SIGNAL_TYPE = [*sorted(CREATING_SIGNALS), "learning"]
_EVERY_COSINE = [0.50, 0.55, 0.60, 0.64, 0.66, 0.69, 0.70, 0.9]


@pytest.mark.asyncio
@pytest.mark.parametrize(
    ("cosine", "signal_type", "expected_action"),
    [(0.65, "snippet", "linked"), (0.6499, "plan", "created"), (0.6499, "learning", "skipped")],
)
async def test_resolve_pins_literal_fallback_boundary(
    mock_deps: dict[str, Any], cosine: float, signal_type: str, expected_action: str
) -> None:
    feature_row = _make_feature_row(similarity=cosine)
    result = MagicMock()
    result.fetchall.return_value = [feature_row]
    created_row = _make_feature_row(name="some signal")
    result.fetchone.return_value = created_row
    mock_deps["session"].execute = AsyncMock(return_value=result)

    feature, action = await _build_guard(mock_deps).resolve(
        text="some signal",
        embedding=[0.1] * 1536,
        project_key="brain_v42",
        signal_type=signal_type,
    )

    assert action == expected_action
    if expected_action == "linked":
        assert feature.id == feature_row.id
    elif expected_action == "created":
        assert feature.id == created_row.id
    else:
        assert feature is None


@pytest.mark.asyncio
@pytest.mark.parametrize("cosine", _EVERY_COSINE)
@pytest.mark.parametrize("signal_type", _EVERY_SIGNAL_TYPE)
async def test_resolve_never_merges_and_never_enriches_a_description(
    mock_deps, signal_type, cosine
):
    """Ruling d4648d84: nothing merges. Whatever the signal type and cosine, the
    action is link/create/skip as the cosine thresholds dictate, and no UPDATE
    ever rewrites a description or an embedding."""
    feature_row = _make_feature_row(similarity=cosine, description="Original description")
    result = MagicMock()
    result.fetchall.return_value = [feature_row]
    result.fetchone.return_value = _make_feature_row(name="some signal")
    mock_deps["session"].execute = AsyncMock(return_value=result)

    guard = _build_guard(mock_deps)
    _, action = await guard.resolve(
        text="some signal",
        embedding=[0.1] * 1536,
        project_key="brain_v42",
        signal_type=signal_type,
    )

    if cosine >= FALLBACK_LINK:
        expected = "linked"
    else:
        expected = "created" if signal_type in CREATING_SIGNALS else "skipped"
    assert action == expected
    assert feature_row.description == "Original description"
    mock_deps["embedding_svc"].embed.assert_not_awaited()
    for call in mock_deps["session"].execute.call_args_list:
        statement = call.args[0]
        if isinstance(statement, sa.sql.dml.Update):
            assert not {"description", "embedding"} & set(statement.compile().params)


@pytest.mark.asyncio
async def test_resolve_creates_when_cosine_below_grey(mock_deps):
    """Cosine < 0.50, work signal (plan) -> created immediately.
    Non-regression: uses a CREATING_SIGNALS type."""
    feature_row = _make_feature_row(similarity=0.35)
    result_set = MagicMock()
    result_set.fetchall.return_value = [feature_row]

    new_feature_row = _make_feature_row(name="totally different")
    insert_result = MagicMock()
    insert_result.fetchone.return_value = new_feature_row

    mock_deps["session"].execute = AsyncMock(side_effect=[MagicMock(), result_set, insert_result])

    guard = _build_guard(mock_deps)
    feature, action = await guard.resolve(
        text="totally different",
        embedding=[0.1] * 1536,
        project_key="brain_v42",
        signal_type="plan",
    )

    assert action == "created"


# ── feature.name sanitization on create ─────────────────────────────────


def _bound_name_from_insert(execute_mock) -> str:
    """Extract the INSERT name without depending on preceding lock statements."""
    insert_stmt = next(
        call.args[0]
        for call in execute_mock.call_args_list
        if isinstance(call.args[0], sa.sql.dml.Insert)
    )
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
    mock_deps["session"].execute = AsyncMock(side_effect=[MagicMock(), result_set, insert_result])

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
    mock_deps["session"].execute = AsyncMock(side_effect=[MagicMock(), result_set, insert_result])

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
    mock_deps["session"].execute = AsyncMock(side_effect=[MagicMock(), result_set, insert_result])

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
    mock_deps["session"].execute = AsyncMock(side_effect=[MagicMock(), result_set, insert_result])

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

    # The candidate query follows the advisory lock.
    first_stmt = mock_deps["session"].execute.call_args_list[1].args[0]
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

    async def execute_and_lose_during_candidates(_statement, *args):
        nonlocal execute_count
        if isinstance(_statement, sa.sql.elements.TextClause):
            return MagicMock()
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
async def test_resolve_does_not_commit_when_ownership_is_lost_during_insert(mock_deps):
    """A post-INSERT lease check must prevent a newly created feature commit."""
    gate = _MutableMutationGate()
    no_candidates = MagicMock()
    no_candidates.fetchall.return_value = []
    inserted = MagicMock()
    inserted.fetchone.return_value = _make_feature_row()
    results = iter((no_candidates, inserted))
    execute_count = 0

    async def execute_and_lose_during_insert(_statement, *args):
        nonlocal execute_count
        if isinstance(_statement, sa.sql.elements.TextClause):
            return MagicMock()
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
