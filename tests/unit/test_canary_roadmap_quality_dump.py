"""The canary counted the proposals and threw away their content.

Its three measurements — valid JSON, seconds per batch, NUMBER of proposals — say
nothing about what is proposed. Two models can return 30 proposals each, one
archiving live features and the other seeing the real duplicates, and the table
would rank them equal. The 2026-08-16 choice was going to be made on that table.

The dump does not measure quality — it makes the content READABLE so that a human
or a judge can measure it. That is deliberate: a quality score returned by the same
layer that produces the proposals would have no arbitration value.

Nothing is persisted: the canary calls neither `persist_proposals` nor
`apply_proposals`, and this dump changes nothing about that.
"""

from __future__ import annotations

from uuid import UUID

from scripts.canary_roadmap_model import _proposals_payload
from scripts.roadmap_curate import BatchOutcome, CurationDraft, FeatureCard, ProjectBatch

_FEATURE_ID = UUID("11111111-1111-1111-1111-111111111111")
_OTHER_ID = UUID("22222222-2222-2222-2222-222222222222")


def _outcome() -> BatchOutcome:
    batch = ProjectBatch(
        project_key="red-lab",
        features=[
            FeatureCard(id=_FEATURE_ID, name="Feature vivante", status="building", pinned=True),
            FeatureCard(id=_OTHER_ID, name="Doublon", status="design", pinned=False),
        ],
    )
    return BatchOutcome(
        batch=batch,
        drafts=[
            CurationDraft(
                op="merge",
                feature_id=_OTHER_ID,
                payload={"into": str(_FEATURE_ID)},
                rationale="même périmètre",
            )
        ],
    )


def test_dump_carries_the_target_feature_not_just_its_uuid() -> None:
    """A bare UUID cannot be judged: the feature the proposal targets is needed.

    Without the target's name, status and pinning, nobody can say whether `archive`
    on this row is a good call or the destruction of a commitment.
    """
    payload = _proposals_payload("mistralai/mistral-nemotron", _outcome())

    proposal = payload["proposals"][0]
    assert proposal["target"]["name"] == "Doublon"
    assert proposal["target"]["status"] == "design"
    assert proposal["target"]["pinned"] is False


def test_dump_keeps_the_rationale_which_is_the_judgeable_part() -> None:
    payload = _proposals_payload("mistralai/mistral-nemotron", _outcome())

    assert payload["proposals"][0]["rationale"] == "même périmètre"
    assert payload["proposals"][0]["op"] == "merge"


def test_merge_names_the_feature_it_would_absorb_into() -> None:
    """`merge` without the absorbing target is unreadable: who eats whom?"""
    payload = _proposals_payload("mistralai/mistral-nemotron", _outcome())

    assert payload["proposals"][0]["payload"]["into_name"] == "Feature vivante"


def test_dump_is_json_serialisable_end_to_end() -> None:
    """The UUIDs must come out as str, otherwise `json.dump` breaks on write."""
    import json

    json.dumps(_proposals_payload("m", _outcome()))


def test_dump_records_the_model_and_the_project_it_ran_on() -> None:
    payload = _proposals_payload("mistralai/mistral-nemotron", _outcome())

    assert payload["model"] == "mistralai/mistral-nemotron"
    assert payload["project_key"] == "red-lab"
