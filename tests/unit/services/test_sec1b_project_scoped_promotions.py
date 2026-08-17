"""Failure-first service propagation tests for SEC1b promotions."""

from __future__ import annotations

import inspect
from datetime import UTC, datetime
from unittest.mock import AsyncMock, MagicMock
from uuid import uuid4

import pytest

from brain_v42.models.adr import ADR, ADRCreate
from brain_v42.models.runbook import Runbook, RunbookCreate, RunbookStep
from brain_v42.services.adr_service import ADRService
from brain_v42.services.runbook_service import RunbookService

PROJECT_KEY = "sec1b-owned"


def adr() -> ADR:
    now = datetime.now(UTC)
    return ADR.model_validate(
        {
            "id": uuid4(),
            "number": 1,
            "title": "Scoped ADR",
            "context": "Context",
            "decision": "Decision",
            "consequences": "Consequences",
            "alternatives_considered": [],
            "project_key": PROJECT_KEY,
            "tags": [],
            "status": "accepted",
            "decided_at": now,
            "superseded_by": None,
            "embedding": None,
            "metadata": {},
            "created_at": now,
            "updated_at": now,
        }
    )


def adr_data() -> ADRCreate:
    return ADRCreate(
        title="Scoped ADR",
        context="Context",
        decision="Decision",
        consequences="Consequences",
        project_key=PROJECT_KEY,
    )


def runbook() -> Runbook:
    now = datetime.now(UTC)
    return Runbook.model_validate(
        {
            "id": uuid4(),
            "title": "Scoped runbook",
            "description": "Description",
            "project_key": PROJECT_KEY,
            "trigger": "Trigger",
            "prerequisites": [],
            "steps": [RunbookStep(order=1, title="Step")],
            "rollback_steps": [],
            "estimated_duration": None,
            "tags": [],
            "metadata": {},
            "execution_count": 0,
            "last_executed_at": None,
            "last_execution_status": None,
            "embedding": None,
            "created_at": now,
            "updated_at": now,
        }
    )


def runbook_data() -> RunbookCreate:
    return RunbookCreate(
        title="Scoped runbook",
        description="Description",
        project_key=PROJECT_KEY,
        trigger="Trigger",
        steps=[RunbookStep(order=1, title="Step")],
    )


@pytest.mark.asyncio
@pytest.mark.parametrize("kind", ["adr", "runbook"])
async def test_scoped_service_passes_authenticated_project_to_repo(kind: str) -> None:
    repo = MagicMock()
    repo.create_with_promotion = AsyncMock(return_value=adr() if kind == "adr" else runbook())
    source_id = uuid4()

    if kind == "adr":
        await ADRService(repo).create_with_promotion(
            adr_data(), source_id, True, project_key=PROJECT_KEY
        )
    else:
        await RunbookService(repo).create_with_promotion(
            runbook_data(), source_id, project_key=PROJECT_KEY
        )

    assert repo.create_with_promotion.await_args.kwargs["project_key"] == PROJECT_KEY


@pytest.mark.asyncio
@pytest.mark.parametrize("kind", ["adr", "runbook"])
async def test_admin_service_omits_scope_kwarg_and_preserves_dream_run(kind: str) -> None:
    repo = MagicMock()
    repo.create_with_promotion = AsyncMock(return_value=adr() if kind == "adr" else runbook())
    source_id = uuid4()

    if kind == "adr":
        await ADRService(repo).create_with_promotion(adr_data(), source_id, True, dream_run_id=73)
        expected = {
            "data",
            "embedding",
            "source_learning_id",
            "auto_accept",
            "dream_run_id",
        }
    else:
        await RunbookService(repo).create_with_promotion(runbook_data(), source_id, dream_run_id=73)
        expected = {"data", "embedding", "source_learning_id", "dream_run_id"}

    assert set(repo.create_with_promotion.await_args.kwargs) == expected
    assert repo.create_with_promotion.await_args.kwargs["dream_run_id"] == 73


def test_service_scope_parameter_is_internal_keyword_only() -> None:
    for method in (ADRService.create_with_promotion, RunbookService.create_with_promotion):
        parameter = inspect.signature(method).parameters["project_key"]
        assert parameter.kind is inspect.Parameter.KEYWORD_ONLY
        assert parameter.default is None
