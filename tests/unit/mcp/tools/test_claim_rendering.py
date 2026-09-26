"""Compact, French, bounded claim suffixes (spec 2026-09-19, section 6.6).

Every string this module returns is rendered to a human operator and stays
French, per the spec's own examples; the module, its tests and this docstring
stay English like the rest of the codebase.
"""

from __future__ import annotations

from dataclasses import replace
from datetime import UTC, datetime
from unittest.mock import AsyncMock
from uuid import UUID, uuid4

from brain_v42.mcp.tools.claim_rendering import (
    CLAIM_SUFFIX_UNAVAILABLE,
    claim_suffix_map,
    format_claim_history,
    format_claim_list,
    render_claim_suffix,
)
from brain_v42.models.claim_read import ClaimRead, ClaimState, VerdictRead, evaluate_claim
from brain_v42.repositories.pg_claim_verdicts import VerdictRow
from brain_v42.services.claim_read_service import ClaimReadError

NOW = datetime(2026, 9, 26, tzinfo=UTC)


def _claim(
    *,
    fact_name: str = "lag",
    expected: dict[str, object] | None = None,
    expected_resolved: dict[str, object] | None = None,
    validity_seconds: int = 60,
) -> ClaimRead:
    return ClaimRead(
        id=uuid4(),
        seq=1,
        entry_id=uuid4(),
        entity_type="learning",
        project_key="project-a",
        claim_key="a" * 64,
        statement="Lag is bounded",
        fact_name=fact_name,
        definition_version=1,
        target="production",
        expected=expected or {"path": "/lag", "op": "lte", "value": 5},
        expected_resolved=expected_resolved or {"path": "/lag", "op": "lte", "value": 5},
        validity_seconds=validity_seconds,
        provenance="declared",
        declared_by="tester",
        declared_at=NOW,
        recorded_at=NOW,
        retired_at=None,
        replaces_id=None,
        latest=None,
        conclusive=None,
    )


def _verdict(
    kind: str,
    seq: int,
    emitted: datetime,
    *,
    reason: str | None = None,
    value_json: dict[str, object] | None = None,
) -> VerdictRead:
    return VerdictRead(
        id=uuid4(),
        seq=seq,
        verdict=kind,
        reason=reason,
        emitted_at=emitted,
        recorded_at=NOW,
        observation_id=uuid4(),
        measurement={"value_json": value_json} if value_json is not None else {},
    )


def _state(
    claim: ClaimRead, latest: VerdictRead | None, conclusive: VerdictRead | None
) -> ClaimState:
    claim = replace(claim, latest=latest, conclusive=conclusive)
    return evaluate_claim(claim, NOW)


def test_no_states_renders_nothing() -> None:
    assert render_claim_suffix([]) is None


def test_a_single_fresh_hold_is_singular() -> None:
    verdict = _verdict("holds", 1, NOW)
    state = _state(_claim(), verdict, verdict)
    assert render_claim_suffix([state]) == "[claims : 1 tient]"


def test_two_holds_are_plural() -> None:
    v1 = _verdict("holds", 1, NOW)
    v2 = _verdict("holds", 2, NOW)
    states = [_state(_claim(), v1, v1), _state(_claim(), v2, v2)]
    assert render_claim_suffix(states) == "[claims : 2 tiennent]"


def test_holds_and_a_singular_falsified_carry_the_spec_example() -> None:
    """Spec 2026-09-19 section 6.6, first canonical example."""
    holds = [_state(_claim(), v, v) for v in (_verdict("holds", 1, NOW), _verdict("holds", 2, NOW))]
    falsified_verdict = _verdict(
        "falsified", 3, datetime(2026, 9, 19, tzinfo=UTC), value_json={"head": "054"}
    )
    falsified_claim = _claim(
        fact_name="alembic_head",
        expected={"path": "/head", "op": "eq", "value": "053"},
        expected_resolved={"path": "/head", "op": "eq", "value": "053"},
        validity_seconds=31_536_000,
    )
    falsified = _state(falsified_claim, falsified_verdict, falsified_verdict)
    suffix = render_claim_suffix([*holds, falsified])
    assert suffix == (
        "[claims : 2 tiennent · 1 FALSIFIÉ le 2026-09-19 (alembic_head mesuré 054, attendu 053)]"
    )


def test_two_falsified_omit_the_detail() -> None:
    v1 = _verdict("falsified", 1, NOW, value_json={"head": "054"})
    v2 = _verdict("falsified", 2, NOW, value_json={"head": "055"})
    claim = _claim(
        fact_name="alembic_head",
        expected={"path": "/head", "op": "eq", "value": "053"},
        expected_resolved={"path": "/head", "op": "eq", "value": "053"},
    )
    states = [_state(claim, v1, v1), _state(claim, v2, v2)]
    assert render_claim_suffix(states) == "[claims : 2 FALSIFIÉS]"


def test_a_singular_stale_hold_carries_the_spec_example() -> None:
    """Spec 2026-09-19 section 6.6, second canonical example."""
    verdict = _verdict("holds", 1, datetime(2026, 9, 1, tzinfo=UTC))
    state = _state(_claim(validity_seconds=1), verdict, verdict)
    assert render_claim_suffix([state]) == "[claims : 1 périmé (tenait le 2026-09-01)]"


def test_a_singular_stale_falsification_names_its_previous_kind() -> None:
    verdict = _verdict("falsified", 1, datetime(2026, 9, 1, tzinfo=UTC), value_json={"head": "054"})
    claim = _claim(
        expected={"path": "/head", "op": "eq", "value": "053"},
        expected_resolved={"path": "/head", "op": "eq", "value": "053"},
        validity_seconds=1,
    )
    state = _state(claim, verdict, verdict)
    assert render_claim_suffix([state]) == "[claims : 1 périmé (était FALSIFIÉ le 2026-09-01)]"


def test_a_singular_unreadable_without_conclusive_carries_the_spec_example() -> None:
    """Spec 2026-09-19 section 6.6, third canonical example."""
    latest = _verdict("unreadable", 1, NOW, reason="target_mismatch")
    state = _state(_claim(), latest, None)
    assert render_claim_suffix([state]) == "[claims : 1 illisible (cible inattendue)]"


def test_an_unverified_declaration_is_named_as_such() -> None:
    state = _state(_claim(), None, None)
    assert render_claim_suffix([state]) == "[claims : 1 non vérifiée]"


def test_a_newer_unreadable_attempt_after_a_fresh_conclusive_stays_visible() -> None:
    """Contract: 'if a newer attempt is unreadable after a conclusive verdict, retain both facts'."""
    conclusive = _verdict("holds", 1, NOW)
    latest = _verdict("unreadable", 2, NOW, reason="probe:timeout")
    state = _state(_claim(), latest, conclusive)
    assert (
        render_claim_suffix([state])
        == "[claims : 1 tient ; dernier essai illisible (sonde timeout)]"
    )


async def test_claim_suffix_map_makes_no_call_for_an_empty_batch() -> None:
    service = AsyncMock()
    result = await claim_suffix_map(service, [])
    assert result == {}
    service.batch_summaries.assert_not_called()


async def test_claim_suffix_map_only_carries_entries_with_an_active_claim() -> None:
    holding = _verdict("holds", 1, NOW)
    key_with_claim = ("learning", uuid4())
    key_without_claim = ("decision", uuid4())
    service = AsyncMock()
    service.batch_summaries.return_value = {
        key_with_claim: (_state(_claim(), holding, holding),),
        key_without_claim: (),
    }

    result = await claim_suffix_map(service, [key_with_claim, key_without_claim])

    assert result == {key_with_claim: "[claims : 1 tient]"}
    service.batch_summaries.assert_awaited_once_with(
        [key_with_claim, key_without_claim], trusted_project_key=None
    )


async def test_claim_suffix_map_marks_every_requested_entry_on_failure_without_silence() -> None:
    entries = [("learning", uuid4()), ("decision", uuid4())]
    service = AsyncMock()
    service.batch_summaries.side_effect = ClaimReadError("read_unavailable")

    result = await claim_suffix_map(service, entries)

    assert result == dict.fromkeys(entries, CLAIM_SUFFIX_UNAVAILABLE)


async def test_claim_suffix_map_threads_the_trusted_project_scope() -> None:
    service = AsyncMock()
    service.batch_summaries.return_value = {}
    entries: list[tuple[str, UUID]] = [("learning", uuid4())]

    await claim_suffix_map(service, entries, trusted_project_key="brain-v42")

    service.batch_summaries.assert_awaited_once_with(entries, trusted_project_key="brain-v42")


def test_format_claim_list_names_the_empty_page() -> None:
    assert format_claim_list([], None) == "## 0 claims"


def test_format_claim_list_renders_one_line_per_occurrence_with_its_state() -> None:
    verdict = _verdict("holds", 1, NOW)
    state = _state(_claim(), verdict, verdict)

    rendered = format_claim_list([state], None)

    assert rendered.startswith("## 1 claim\n")
    assert str(state.claim.id) in rendered
    assert state.claim.statement in rendered
    assert "1 tient" in rendered
    assert "[retired]" not in rendered


def test_format_claim_list_names_a_retired_occurrence_and_the_next_page() -> None:
    verdict = _verdict("holds", 1, NOW)
    claim = replace(_claim(), retired_at=NOW)
    state = _state(claim, verdict, verdict)

    rendered = format_claim_list([state], 41)

    assert "[retired]" in rendered
    assert "after_seq=41" in rendered


def test_format_claim_history_renders_claim_state_and_every_verdict_in_order() -> None:
    verdict = _verdict("holds", 1, NOW)
    state = _state(_claim(), verdict, verdict)
    rows = (
        VerdictRow(
            id=uuid4(),
            seq=1,
            claim_id=state.claim.id,
            verdict="unreadable",
            reason="probe:timeout",
            measurement={},
            measurement_digest=None,
            observation_id=uuid4(),
            issuer_identity="mcp:codex",
            issuer_kind="robot",
            request_fingerprint="r" * 64,
            outcome_fingerprint="o" * 64,
            idempotency_key="k1",
            emitted_at=NOW,
            recorded_at=NOW,
        ),
    )

    rendered = format_claim_history(state, rows, None)

    assert str(state.claim.id) in rendered
    assert "1 tient" in rendered
    assert "seq 1" in rendered
    assert "probe:timeout" in rendered


def test_format_claim_history_names_an_empty_history_as_a_valid_result() -> None:
    verdict = _verdict("holds", 1, NOW)
    state = _state(_claim(), verdict, verdict)

    rendered = format_claim_history(state, (), 7)

    assert "(empty)" in rendered
    assert "after_seq=7" in rendered
