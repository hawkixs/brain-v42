"""Unit tests for GitLabIngestor — processes GitLab webhook payloads."""

from __future__ import annotations

import uuid
from unittest.mock import AsyncMock, MagicMock

import pytest

from brain_v42.automation.ownership import OwnershipLostError
from brain_v42.services.gitlab_ingestor import GitLabIngestor

# ── fixtures ────────────────────────────────────────────────────────────


@pytest.fixture
def mock_deps():
    """Create mock dependencies for GitLabIngestor."""
    session = AsyncMock()
    factory = MagicMock()
    factory.return_value.__aenter__ = AsyncMock(return_value=session)
    factory.return_value.__aexit__ = AsyncMock(return_value=False)

    embedding_svc = AsyncMock()
    embedding_svc.embed = AsyncMock(return_value=[0.1] * 1536)

    cluster_guard = AsyncMock()
    mock_feature = MagicMock(id=uuid.uuid4(), name="Test Feature")
    cluster_guard.resolve = AsyncMock(return_value=(mock_feature, "linked"))

    return {
        "session_factory": factory,
        "session": session,
        "embedding_svc": embedding_svc,
        "cluster_guard": cluster_guard,
        "mock_feature": mock_feature,
    }


def _build_ingestor(deps: dict) -> GitLabIngestor:
    return GitLabIngestor(
        session_factory=deps["session_factory"],
        embedding_svc=deps["embedding_svc"],
        cluster_guard=deps["cluster_guard"],
    )


def _mr_open_payload(
    *,
    title: str = "feat: add decay system",
    description: str = "Implement the memory decay feature",
    source_branch: str = "feat/decay-system",
    action: str = "open",
) -> dict:
    """Build a merge_request webhook payload."""
    return {
        "object_kind": "merge_request",
        "object_attributes": {
            "action": action,
            "title": title,
            "description": description,
            "source_branch": source_branch,
        },
    }


def _push_payload(
    *,
    ref: str = "refs/heads/feat/decay-system",
    commits: list[dict] | None = None,
) -> dict:
    """Build a push webhook payload."""
    if commits is None:
        commits = [
            {"message": "Add decay scoring logic"},
            {"message": "Fix edge case in scorer"},
        ]
    return {
        "object_kind": "push",
        "ref": ref,
        "commits": commits,
    }


def _pipeline_payload(*, status: str = "success", ref: str = "main") -> dict:
    """Build a pipeline webhook payload."""
    return {
        "object_kind": "pipeline",
        "object_attributes": {
            "status": status,
            "ref": ref,
        },
    }


def _mock_duplicate_check_no_existing(mock_deps):
    """Set up session to return no existing row for duplicate check."""
    # 1. Duplicate check: SELECT -> fetchone() returns None
    dup_result = MagicMock()
    dup_result.fetchone.return_value = None

    # 2. Store event: INSERT RETURNING -> fetchone() returns row with .id
    event_id = uuid.uuid4()
    event_row = MagicMock()
    event_row.id = event_id
    insert_result = MagicMock()
    insert_result.fetchone.return_value = event_row

    # 3. Link to feature: INSERT on_conflict_do_nothing
    link_result = MagicMock()

    mock_deps["session"].execute = AsyncMock(side_effect=[dup_result, insert_result, link_result])
    return event_id


# ── tests ───────────────────────────────────────────────────────────────


@pytest.mark.asyncio
async def test_process_mr_open_event(mock_deps):
    """MR opened: should embed, call cluster_guard, return processed."""
    event_uuid = str(uuid.uuid4())
    _mock_duplicate_check_no_existing(mock_deps)

    ingestor = _build_ingestor(mock_deps)
    result = await ingestor.process_event(
        payload=_mr_open_payload(),
        event_uuid=event_uuid,
        project_key="brain_v42",
    )

    assert result["status"] == "processed"
    assert result["signal_type"] == "mr_opened"
    mock_deps["embedding_svc"].embed.assert_awaited_once()
    mock_deps["cluster_guard"].resolve.assert_awaited_once()


@pytest.mark.asyncio
async def test_process_push_event(mock_deps):
    """Push event with commits: should extract text from branch + commits."""
    event_uuid = str(uuid.uuid4())
    _mock_duplicate_check_no_existing(mock_deps)

    ingestor = _build_ingestor(mock_deps)
    result = await ingestor.process_event(
        payload=_push_payload(),
        event_uuid=event_uuid,
        project_key="brain_v42",
    )

    assert result["status"] == "processed"
    assert result["signal_type"] == "push"
    mock_deps["embedding_svc"].embed.assert_awaited_once()

    # Verify embedded text contains commit messages
    embed_call_text = mock_deps["embedding_svc"].embed.call_args[0][0]
    assert "Add decay scoring logic" in embed_call_text
    assert "Fix edge case in scorer" in embed_call_text


@pytest.mark.asyncio
async def test_extract_text_from_mr(mock_deps):
    """MR text extraction: title (stripped prefix) + description[:500] + branch."""
    ingestor = _build_ingestor(mock_deps)

    payload = _mr_open_payload(
        title="feat: add decay system",
        description="Implement the memory decay feature with scoring",
        source_branch="feat/decay-system",
    )
    text = ingestor._extract_text(payload)

    # Title should have feat: prefix stripped
    assert "add decay system" in text
    assert "feat:" not in text
    # Description included
    assert "Implement the memory decay feature with scoring" in text
    # Branch included
    assert "feat/decay-system" in text


@pytest.mark.asyncio
async def test_extract_feature_name_from_branch(mock_deps):
    """Branch parsing: refs/heads/feat/decay-system -> Decay System."""
    ingestor = _build_ingestor(mock_deps)

    assert ingestor._branch_to_feature_name("refs/heads/feat/decay-system") == "Decay System"
    assert ingestor._branch_to_feature_name("refs/heads/fix/memory-leak") == "Memory Leak"
    assert ingestor._branch_to_feature_name("refs/heads/feature/cool-thing") == "Cool Thing"
    assert ingestor._branch_to_feature_name("refs/heads/hotfix/urgent-fix") == "Urgent Fix"
    assert ingestor._branch_to_feature_name("refs/heads/bugfix/off-by-one") == "Off By One"
    # Non-matching branch returns None
    assert ingestor._branch_to_feature_name("refs/heads/main") is None
    assert ingestor._branch_to_feature_name("refs/heads/dev") is None


@pytest.mark.asyncio
async def test_skips_duplicate_event(mock_deps):
    """Duplicate gitlab_event_id returns skipped_duplicate."""
    event_uuid = str(uuid.uuid4())

    # Duplicate check returns existing row
    existing_row = MagicMock()
    existing_row.id = uuid.uuid4()
    dup_result = MagicMock()
    dup_result.fetchone.return_value = existing_row
    mock_deps["session"].execute = AsyncMock(return_value=dup_result)

    ingestor = _build_ingestor(mock_deps)
    result = await ingestor.process_event(
        payload=_mr_open_payload(),
        event_uuid=event_uuid,
        project_key="brain_v42",
    )

    assert result["status"] == "skipped_duplicate"
    # Should NOT embed or call cluster_guard
    mock_deps["embedding_svc"].embed.assert_not_awaited()
    mock_deps["cluster_guard"].resolve.assert_not_awaited()


@pytest.mark.asyncio
async def test_process_mr_merge_event(mock_deps):
    """MR merge: should return mr_merged signal type."""
    event_uuid = str(uuid.uuid4())
    _mock_duplicate_check_no_existing(mock_deps)

    ingestor = _build_ingestor(mock_deps)
    result = await ingestor.process_event(
        payload=_mr_open_payload(action="merge"),
        event_uuid=event_uuid,
        project_key="brain_v42",
    )

    assert result["status"] == "processed"
    assert result["signal_type"] == "mr_merged"


@pytest.mark.asyncio
async def test_process_mr_close_event(mock_deps):
    """MR close: should return mr_closed signal, still processes (linking only)."""
    event_uuid = str(uuid.uuid4())
    _mock_duplicate_check_no_existing(mock_deps)

    ingestor = _build_ingestor(mock_deps)
    result = await ingestor.process_event(
        payload=_mr_open_payload(action="close"),
        event_uuid=event_uuid,
        project_key="brain_v42",
    )

    assert result["status"] == "processed"
    assert result["signal_type"] == "mr_closed"


@pytest.mark.asyncio
async def test_process_pipeline_success(mock_deps):
    """Pipeline success: should embed pipeline ref text."""
    event_uuid = str(uuid.uuid4())
    _mock_duplicate_check_no_existing(mock_deps)

    ingestor = _build_ingestor(mock_deps)
    result = await ingestor.process_event(
        payload=_pipeline_payload(status="success", ref="main"),
        event_uuid=event_uuid,
        project_key="brain_v42",
    )

    assert result["status"] == "processed"
    assert result["signal_type"] == "pipeline_success"

    # Verify embedded text contains the ref
    embed_call_text = mock_deps["embedding_svc"].embed.call_args[0][0]
    assert "Pipeline success: main" in embed_call_text


@pytest.mark.asyncio
async def test_process_pipeline_failure_returns_immediately(mock_deps):
    """Pipeline failure: returns immediately, no embedding or linking."""
    event_uuid = str(uuid.uuid4())

    # Only need the duplicate check (no existing)
    dup_result = MagicMock()
    dup_result.fetchone.return_value = None
    mock_deps["session"].execute = AsyncMock(return_value=dup_result)

    ingestor = _build_ingestor(mock_deps)
    result = await ingestor.process_event(
        payload=_pipeline_payload(status="failed", ref="main"),
        event_uuid=event_uuid,
        project_key="brain_v42",
    )

    assert result["status"] == "skipped_pipeline_failure"
    mock_deps["embedding_svc"].embed.assert_not_awaited()
    mock_deps["cluster_guard"].resolve.assert_not_awaited()


@pytest.mark.asyncio
async def test_mr_title_strip_fix_prefix(mock_deps):
    """MR title with fix: prefix should be stripped."""
    ingestor = _build_ingestor(mock_deps)

    payload = _mr_open_payload(title="fix: resolve memory leak")
    text = ingestor._extract_text(payload)

    assert "resolve memory leak" in text
    assert "fix:" not in text


@pytest.mark.asyncio
async def test_push_text_includes_branch_feature_name(mock_deps):
    """Push text should include parsed feature name from branch."""
    ingestor = _build_ingestor(mock_deps)

    payload = _push_payload(ref="refs/heads/feat/decay-system")
    text = ingestor._extract_text(payload)

    assert "Decay System" in text


# ── embed failure graceful degradation ─────────────────────────────────


@pytest.mark.asyncio
async def test_process_event_returns_skipped_when_embed_fails(mock_deps):
    """When embedding service is down, process_event returns skipped status."""
    # _is_duplicate returns False
    dup_result = MagicMock()
    dup_result.fetchone.return_value = None
    mock_deps["session"].execute = AsyncMock(return_value=dup_result)

    # Embedding service raises
    mock_deps["embedding_svc"].embed = AsyncMock(side_effect=Exception("GPU down"))

    ingestor = _build_ingestor(mock_deps)
    result = await ingestor.process_event(
        payload=_mr_open_payload(),
        event_uuid=str(uuid.uuid4()),
        project_key="brain_v42",
    )

    assert result["status"] == "skipped_embed_failed"
    # ClusterGuard should NOT be called
    mock_deps["cluster_guard"].resolve.assert_not_called()


class _MutableMutationGate:
    """Deterministic ownership probe used at async mutation boundaries."""

    def __init__(self) -> None:
        self.owned = True

    def ensure_owned(self) -> None:
        if not self.owned:
            raise OwnershipLostError("ownership lost during webhook processing")


@pytest.mark.asyncio
async def test_process_event_stops_after_ownership_loss_during_embedding(mock_deps):
    """An embedding await may not admit ClusterGuard work after lease loss."""
    gate = _MutableMutationGate()
    duplicate_result = MagicMock()
    duplicate_result.fetchone.return_value = None
    mock_deps["session"].execute = AsyncMock(return_value=duplicate_result)

    async def embed_after_losing_ownership(_text: str) -> list[float]:
        gate.owned = False
        return [0.1] * 1536

    mock_deps["embedding_svc"].embed = AsyncMock(side_effect=embed_after_losing_ownership)
    ingestor = _build_ingestor(mock_deps)
    # Tests inject the wished-for seam on the RED baseline so this exercises
    # the in-flight behavior rather than failing on a constructor TypeError.
    ingestor._mutation_guard = gate.ensure_owned  # type: ignore[attr-defined]

    with pytest.raises(OwnershipLostError, match="during webhook processing"):
        await ingestor.process_event(
            payload=_mr_open_payload(),
            event_uuid=str(uuid.uuid4()),
            project_key="brain_v42",
        )

    mock_deps["cluster_guard"].resolve.assert_not_awaited()
    mock_deps["session"].commit.assert_not_awaited()


@pytest.mark.asyncio
async def test_store_event_does_not_commit_when_ownership_is_lost_during_insert(mock_deps):
    """A lease check after INSERT must prevent its transaction from committing."""
    gate = _MutableMutationGate()
    duplicate_result = MagicMock()
    duplicate_result.fetchone.return_value = None
    event_row = MagicMock(id=uuid.uuid4())
    insert_result = MagicMock()
    insert_result.fetchone.return_value = event_row
    link_result = MagicMock()
    results = iter((duplicate_result, insert_result, link_result))
    execute_count = 0

    async def execute_and_lose_on_event_insert(_statement):
        nonlocal execute_count
        execute_count += 1
        result = next(results)
        if execute_count == 2:
            gate.owned = False
        return result

    mock_deps["session"].execute = AsyncMock(side_effect=execute_and_lose_on_event_insert)
    ingestor = _build_ingestor(mock_deps)
    ingestor._mutation_guard = gate.ensure_owned  # type: ignore[attr-defined]

    with pytest.raises(OwnershipLostError, match="during webhook processing"):
        await ingestor.process_event(
            payload=_mr_open_payload(),
            event_uuid=str(uuid.uuid4()),
            project_key="brain_v42",
        )

    mock_deps["session"].commit.assert_not_awaited()


@pytest.mark.asyncio
async def test_process_event_fails_closed_when_ownership_is_lost_after_final_link(mock_deps):
    """The final artifact await must be followed by an ownership check."""
    gate = _MutableMutationGate()
    ingestor = _build_ingestor(mock_deps)
    ingestor._mutation_guard = gate.ensure_owned  # type: ignore[attr-defined]
    ingestor._is_duplicate = AsyncMock(return_value=False)  # type: ignore[method-assign]
    ingestor._store_event = AsyncMock(return_value=uuid.uuid4())  # type: ignore[method-assign]

    async def link_then_lose_ownership(*_args, **_kwargs) -> None:
        gate.owned = False

    ingestor._link_to_feature = AsyncMock(  # type: ignore[method-assign]
        side_effect=link_then_lose_ownership
    )

    with pytest.raises(OwnershipLostError, match="during webhook processing"):
        await ingestor.process_event(
            payload=_mr_open_payload(),
            event_uuid=str(uuid.uuid4()),
            project_key="brain_v42",
        )
