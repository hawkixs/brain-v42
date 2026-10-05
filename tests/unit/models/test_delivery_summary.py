"""The list projection retains claim inputs without carrying immutable proof bodies."""

import json

import pytest
from pydantic import ValidationError

from brain_v42.models.delivery import (
    DeliveryFinding,
    DeliveryListPage,
    DeliveryPage,
    DeliveryView,
    summarize_view,
)
from brain_v42.models.delivery_evaluator import evaluate_delivery
from tests.delivery_helpers import FIXED_NOW, delivery_inputs


def summary_view() -> DeliveryView:
    inputs = delivery_inputs(required_check="failure")
    return DeliveryView(
        contract=inputs.contract,
        assessment=evaluate_delivery(inputs, now=FIXED_NOW),
        bindings=inputs.active_bindings,
        contexts=inputs.contexts,
    )


def test_summary_keeps_claim_inputs() -> None:
    view = summary_view()
    summary = summarize_view(view, include_view=False)
    for name in (
        "assessment_id",
        "assessment_version",
        "assessed_at",
        "observed_at",
        "fresh_until",
        "coordination_status",
        "delivery_stage",
        "observation_health",
        "acceptance_state",
        "requirements_satisfied",
        "integration_receipt_eligible",
        "completion_eligible_now",
        "contract_fulfilled",
        "delivery_digest",
        "eligible_work",
    ):
        assert getattr(summary, name) == getattr(view.assessment, name)
    assert [item.code for item in summary.blockers] == [
        item.code for item in view.assessment.blockers
    ]
    assert summary.ticket_id == view.contract.ticket_id
    assert summary.contract_revision == view.contract.contract_revision
    assert summary.author_project == view.contract.author_project
    assert summary.context_count == len(view.contexts)
    assert summary.contexts_available == 1
    binding = summary.bindings[0]
    assert binding.deliverable_key == view.bindings[0].binding.deliverable_key
    assert binding.head_sha == view.bindings[0].binding.head_sha
    assert binding.last_attempt_outcome == view.bindings[0].last_attempt_outcome


def test_summary_drops_heavy_nesting() -> None:
    payload = summarize_view(summary_view(), include_view=False).model_dump(mode="json")

    def check(value: object) -> None:
        if isinstance(value, dict):
            assert not {"content_snapshot", "evidence", "proof"} & value.keys()
            for child in value.values():
                check(child)
        elif isinstance(value, list):
            for child in value:
                check(child)

    check(payload)
    assert payload["view"] is None


@pytest.mark.parametrize("blocker_count", [0, 32, 40])
def test_summary_reports_omitted_blockers(blocker_count: int) -> None:
    view = summary_view()
    blockers = tuple(
        DeliveryFinding(code=f"blocker_{i}", detail="Blocker explanation")
        for i in range(blocker_count)
    )
    view = view.model_copy(
        update={"assessment": view.assessment.model_copy(update={"blockers": blockers})}
    )

    summary = summarize_view(view, include_view=False)

    assert len(summary.blockers) == min(blocker_count, 32)
    assert tuple(item.code for item in summary.blockers) == tuple(
        item.code for item in blockers[:32]
    )
    assert summary.blockers_omitted == max(blocker_count - 32, 0)
    assert summary.model_dump(mode="json")["blockers_omitted"] == max(blocker_count - 32, 0)


def test_summary_page_is_bounded() -> None:
    view = summary_view()
    pinned = view.contract.context_refs[0].model_copy(update={"content_snapshot": "x" * 60_000})
    # Model copies preserve the previously validated identities while enlarging payload bodies.
    heavy = view.model_copy(
        update={
            "contract": view.contract.model_copy(update={"context_refs": (pinned,)}),
            "bindings": view.bindings * 3,
            "assessment": view.assessment.model_copy(
                update={
                    "blockers": tuple(
                        DeliveryFinding(code=f"blocker_{i}", detail="x" * 1000) for i in range(32)
                    )
                }
            ),
        }
    )
    full = DeliveryPage(items=(heavy,) * 20, next_cursor="next", omitted_count=7)
    compact = DeliveryListPage(
        items=tuple(summarize_view(item, include_view=False) for item in full.items),
        next_cursor=full.next_cursor,
        omitted_count=full.omitted_count,
    )
    size = len(compact.model_dump_json().encode())
    assert size < 64 * 1024
    assert size < len(full.model_dump_json().encode()) / 10
    assert compact.next_cursor == "next" and compact.omitted_count == 7


def test_full_detail_embeds_the_view_unchanged() -> None:
    view = summary_view()
    result = summarize_view(view, include_view=True)
    assert result.view == view
    assert json.loads(result.model_dump_json())["view"] == view.model_dump(mode="json")


def test_summary_page_rejects_more_than_one_hundred_items() -> None:
    summary = summarize_view(summary_view(), include_view=False)
    with pytest.raises(ValidationError):
        DeliveryListPage(items=(summary,) * 101)
