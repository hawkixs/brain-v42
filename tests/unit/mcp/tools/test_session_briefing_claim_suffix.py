"""Session briefing recap's compact claim suffix (spec 2026-09-19, section 6.6).

The session briefing shares the existing briefing loader (`make_session_briefing_loader`):
decisions and learnings in the ### Recap section get the same batched claim suffix
brain_get and brain_search already append -- one batch fetch for the WHOLE recap
(never per-entry), and a lookup failure renders a visible marker instead of dropping
the briefing. No extra Brain session lifecycle call is issued for this.
"""

from __future__ import annotations

from datetime import UTC, datetime
from types import SimpleNamespace
from typing import Any
from unittest.mock import AsyncMock, MagicMock
from uuid import uuid4

from brain_v42.mcp.tools.claim_rendering import CLAIM_SUFFIX_UNAVAILABLE
from brain_v42.mcp.tools.session_tools import make_session_briefing_loader
from brain_v42.models.claim_read import ClaimRead, VerdictRead, evaluate_claim
from brain_v42.models.decision import Decision
from brain_v42.models.learning import Learning
from brain_v42.services.claim_read_service import ClaimReadError
from brain_v42.services.dream_run_service import KillswitchState


def _killswitches() -> KillswitchState:
    return KillswitchState(
        last_run_date=None,
        promote_enabled=False,
        promote_dry=False,
        reorg_enabled=False,
        reorg_dry=False,
        promote_clean_dry_nights=0,
        reorg_clean_dry_nights=0,
    )


def _decision(**overrides: Any) -> Decision:
    defaults: dict[str, Any] = {
        "id": uuid4(),
        "title": "Use RRF fusion",
        "description": "desc",
        "reasoning": "reason",
        "project_key": "brain-v42",
        "status": "active",
        "created_at": datetime.now(UTC),
        "updated_at": datetime.now(UTC),
    }
    defaults.update(overrides)
    return Decision.model_validate(defaults)


def _learning(**overrides: Any) -> Learning:
    defaults: dict[str, Any] = {
        "id": uuid4(),
        "topic": "RRF",
        "insight": "Reciprocal Rank Fusion works well",
        "project_key": "brain-v42",
        "created_at": datetime.now(UTC),
        "updated_at": datetime.now(UTC),
    }
    defaults.update(overrides)
    return Learning.model_validate(defaults)


def _state(entry_id: Any, entity_type: str) -> Any:
    now = datetime(2026, 9, 26, tzinfo=UTC)
    verdict = VerdictRead(
        id=uuid4(),
        seq=1,
        verdict="holds",
        reason=None,
        emitted_at=now,
        recorded_at=now,
        observation_id=uuid4(),
        measurement={},
    )
    claim = ClaimRead(
        id=uuid4(),
        seq=1,
        entry_id=entry_id,
        entity_type=entity_type,
        project_key="brain-v42",
        claim_key="a" * 64,
        statement="Lag is bounded",
        fact_name="lag",
        definition_version=1,
        target="production",
        expected={"path": "/lag", "op": "lte", "value": 5},
        expected_resolved={"path": "/lag", "op": "lte", "value": 5},
        validity_seconds=60,
        provenance="declared",
        declared_by="tester",
        declared_at=now,
        recorded_at=now,
        retired_at=None,
        replaces_id=None,
        latest=verdict,
        conclusive=verdict,
    )
    return evaluate_claim(claim, now)


def _loader_services(
    *, decisions: list[Any], learnings: list[Any]
) -> tuple[MagicMock, MagicMock, MagicMock, MagicMock, MagicMock, MagicMock]:
    """Provide only the independent reads a briefing composes (mirrors test_briefing_facts.py)."""
    context = MagicMock()
    context.get_by_key = AsyncMock(
        return_value=SimpleNamespace(
            project_key="brain-v42", current_focus="ship it", blockers=[], focus_updated_at=None
        )
    )
    decision_svc = MagicMock()
    decision_svc.list_all = AsyncMock(return_value=decisions)
    learning_svc = MagicMock()
    learning_svc.list_all = AsyncMock(return_value=learnings)
    dreams = MagicMock()
    dreams.killswitch_state = AsyncMock(return_value=_killswitches())
    dreams.last_failure = AsyncMock(return_value=None)
    features = MagicMock()
    features.roadmap_alive = AsyncMock(return_value=[])
    features.stale_pinned = AsyncMock(return_value=[])
    sessions = MagicMock()
    sessions.recent_checkpoints = AsyncMock(return_value=[])
    return context, decision_svc, learning_svc, dreams, features, sessions


async def test_recap_appends_the_suffix_for_an_active_claim() -> None:
    decision = _decision()
    learning = _learning()
    claim_svc = AsyncMock()
    claim_svc.batch_summaries.return_value = {
        ("decision", decision.id): (_state(decision.id, "decision"),),
        ("learning", learning.id): (),
    }
    loader = make_session_briefing_loader(
        *_loader_services(decisions=[decision], learnings=[learning]),
        claim_read_svc=claim_svc,
    )

    briefing = await loader("brain-v42", uuid4())

    recap = briefing.split("### Recap\n", 1)[1].split("\n\n", 1)[0]
    assert f"- d: {decision.title} [claims : 1 tient]" in recap
    assert f"- l: {learning.topic}: {learning.insight}" in recap
    claim_svc.batch_summaries.assert_awaited_once_with(
        [("decision", decision.id), ("learning", learning.id)],
        trusted_project_key="brain-v42",
    )


async def test_recap_shows_a_visible_marker_when_the_lookup_fails() -> None:
    """A lookup failure never drops the briefing -- it renders a visible marker."""
    decision = _decision()
    claim_svc = AsyncMock()
    claim_svc.batch_summaries.side_effect = ClaimReadError("read_unavailable")
    loader = make_session_briefing_loader(
        *_loader_services(decisions=[decision], learnings=[]),
        claim_read_svc=claim_svc,
    )

    briefing = await loader("brain-v42", uuid4())

    assert decision.title in briefing
    assert CLAIM_SUFFIX_UNAVAILABLE in briefing


async def test_no_active_claims_is_byte_identical_to_no_claim_read_svc() -> None:
    """No active claim renders nothing -- the pre-claims briefing stays byte-identical."""
    decision = _decision()
    learning = _learning()
    claim_svc = AsyncMock()
    claim_svc.batch_summaries.return_value = {
        ("decision", decision.id): (),
        ("learning", learning.id): (),
    }
    with_svc = await make_session_briefing_loader(
        *_loader_services(decisions=[decision], learnings=[learning]),
        claim_read_svc=claim_svc,
    )("brain-v42", uuid4())
    without_svc = await make_session_briefing_loader(
        *_loader_services(decisions=[decision], learnings=[learning])
    )("brain-v42", uuid4())

    assert with_svc == without_svc


async def test_omitting_claim_read_svc_issues_no_claim_lookup() -> None:
    """The optional dependency default keeps the pre-claims briefing loader intact."""
    decision = _decision()
    loader = make_session_briefing_loader(*_loader_services(decisions=[decision], learnings=[]))

    briefing = await loader("brain-v42", uuid4())

    assert "[claims" not in briefing
