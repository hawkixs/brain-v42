"""Validation contract for explicit roadmap feature creation."""

from __future__ import annotations

import pytest
from pydantic import ValidationError

from brain_v42.models.feature import FeatureCreate


def test_feature_create_normalizes_text_and_defaults_to_visible_planned_work() -> None:
    feature = FeatureCreate(
        project_key="brain_v42",
        name="  Explicit roadmap feature  ",
        description="  Deliver a fail-closed creation tool.  ",
    )

    assert feature.project_key == "brain-v42"
    assert feature.name == "Explicit roadmap feature"
    assert feature.description == "Deliver a fail-closed creation tool."
    assert feature.status == "planned"
    assert feature.pinned is True


def test_feature_create_applies_length_limits_after_trimming() -> None:
    feature = FeatureCreate(
        project_key="brain-v42",
        name=f"  {'n' * 200}  ",
        description=f"\n{'d' * 10_000}\t",
    )

    assert feature.name == "n" * 200
    assert feature.description == "d" * 10_000


def test_feature_create_schema_exposes_the_creatable_status_enum() -> None:
    status_schema = FeatureCreate.model_json_schema()["properties"]["status"]

    assert status_schema["enum"] == [
        "planned",
        "research",
        "design",
        "building",
        "deployed",
        "done",
    ]


@pytest.mark.parametrize(
    "status",
    ["planned", "research", "design", "building", "deployed", "done"],
)
def test_feature_create_accepts_each_live_initial_status(status: str) -> None:
    feature = FeatureCreate(
        project_key="brain-v42",
        name="Roadmap feature",
        description="Useful description",
        status=status,
    )

    assert feature.status == status


@pytest.mark.parametrize(
    ("field", "value"),
    [
        ("name", "   "),
        ("description", "\n\t"),
        ("name", "x" * 201),
        ("description", "x" * 10_001),
    ],
)
def test_feature_create_rejects_blank_or_oversized_text(field: str, value: str) -> None:
    payload = {
        "project_key": "brain-v42",
        "name": "Roadmap feature",
        "description": "Useful description",
        field: value,
    }

    with pytest.raises(ValidationError):
        FeatureCreate(**payload)


@pytest.mark.parametrize("status", ["archived", "shipped"])
def test_feature_create_rejects_non_creatable_status(status: str) -> None:
    with pytest.raises(ValidationError):
        FeatureCreate(
            project_key="brain-v42",
            name="Roadmap feature",
            description="Useful description",
            status=status,
        )
