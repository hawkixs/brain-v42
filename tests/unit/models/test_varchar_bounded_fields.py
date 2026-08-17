"""Over-long values must be refused at validation, not at INSERT.

Regression pin for the incident of 2026-08-06 (tickets 39cc4986 and, from
red-shrik, 2af71e69). The generic guard lives in
``tests/unit/test_model_column_width_contract.py`` and proves the constraints
*exist*; this module proves what the caller actually experiences — a named
field in a ``ValidationError`` instead of an opaque failure raised by
PostgreSQL after the request already reached the database.

The runbook boundary is the exact one red-shrik bisected: 50 characters was
accepted, 51 produced ``Error calling tool 'brain_update'`` with nothing to act
on. The value that triggered the incident was 58 characters:
``5-10 min (dont ~1 min de téléchargement du binaire ~290 Mo)``.
"""

from __future__ import annotations

import pytest
from pydantic import ValidationError

from brain_v42.models.decision import DecisionUpdate
from brain_v42.models.learning import LearningUpdate
from brain_v42.models.project_context import ProjectContextCreate, ProjectContextUpdate
from brain_v42.models.runbook import RunbookCreate, RunbookUpdate
from brain_v42.models.snippet import SnippetUpdate

_INCIDENT_VALUE = "5-10 min (dont ~1 min de téléchargement du binaire ~290 Mo)"


def _runbook_create(**overrides: object) -> RunbookCreate:
    payload: dict[str, object] = {
        "title": "t",
        "description": "d",
        "project_key": "brain-v42",
        "trigger": "manual",
    }
    payload.update(overrides)
    return RunbookCreate(**payload)  # type: ignore[arg-type]


def test_runbook_accepts_estimated_duration_at_the_column_width() -> None:
    """50 characters is the documented boundary and must stay accepted."""
    runbook = _runbook_create(estimated_duration="x" * 50)
    assert runbook.estimated_duration is not None
    assert len(runbook.estimated_duration) == 50


def test_runbook_create_rejects_estimated_duration_past_the_column_width() -> None:
    """51 characters is where red-shrik's bisection flipped."""
    with pytest.raises(ValidationError) as exc_info:
        _runbook_create(estimated_duration="x" * 51)

    assert "estimated_duration" in str(exc_info.value)


def test_runbook_update_rejects_the_value_that_caused_the_incident() -> None:
    """The real 58-character duration must now fail with a named field."""
    with pytest.raises(ValidationError) as exc_info:
        RunbookUpdate(estimated_duration=_INCIDENT_VALUE)

    assert "estimated_duration" in str(exc_info.value)


@pytest.mark.parametrize(
    ("model", "field"),
    [
        pytest.param(DecisionUpdate, "project_key", id="decision-update"),
        pytest.param(LearningUpdate, "project_key", id="learning-update"),
        pytest.param(SnippetUpdate, "project_key", id="snippet-update"),
        pytest.param(ProjectContextUpdate, "project_group", id="context-update-group"),
    ],
)
def test_update_models_reject_over_long_project_scoped_keys(model: type, field: str) -> None:
    """The Update models had been forgotten while their Create twins were bounded."""
    with pytest.raises(ValidationError) as exc_info:
        model(**{field: "x" * 51})

    assert field in str(exc_info.value)


@pytest.mark.parametrize(
    "model",
    [
        pytest.param(ProjectContextCreate, id="create"),
        pytest.param(ProjectContextUpdate, id="update"),
    ],
)
def test_project_context_rejects_over_long_gitlab_path(model: type) -> None:
    """gitlab_project_path is VARCHAR(200) on both write models."""
    payload: dict[str, object] = {"gitlab_project_path": "x" * 201}
    if model is ProjectContextCreate:
        payload |= {"project_key": "brain-v42", "name": "brain"}

    with pytest.raises(ValidationError) as exc_info:
        model(**payload)

    assert "gitlab_project_path" in str(exc_info.value)
