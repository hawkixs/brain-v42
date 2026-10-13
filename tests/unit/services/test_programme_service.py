"""ProgrammeService: who is calling comes from the credential, never from an argument.

Contract docs/contracts/programmes_v1.md §6: the caller's project, as the credential
layer resolved it, signs every write and decides its authority. A call with no
verified principal or no resolved project is refused before the repository is
touched. Every input is validated here, so the repository only sees clean values.
"""

from __future__ import annotations

from collections.abc import Iterator
from typing import Any
from unittest.mock import AsyncMock

import pytest

from brain_v42.models.programme import ProgrammeError
from brain_v42.provenance import (
    set_current_actor_project,
    set_current_principal,
)
from brain_v42.services.programme_service import ProgrammeService


@pytest.fixture
def repo() -> AsyncMock:
    fake = AsyncMock()
    for name in ("upsert_programme", "upsert_gate", "report_gate", "request", "list_programmes"):
        getattr(fake, name).return_value = {"key": "move"}
    return fake


@pytest.fixture
def as_project() -> Iterator[Any]:
    def bind(principal: str | None, project: str | None) -> None:
        set_current_principal(principal)
        set_current_actor_project(project)

    yield bind
    bind(None, None)


async def test_the_caller_is_the_credentials_project(repo: AsyncMock, as_project: Any) -> None:
    as_project("workstation-claude", "red-backup")
    await ProgrammeService(repo).report_gate(programme="move", gate="G7", state="done")
    assert repo.report_gate.await_args.kwargs["caller"] == "red-backup"


@pytest.mark.parametrize(
    ("principal", "project"),
    [(None, None), (None, "red-backup"), ("workstation-claude", None)],
)
async def test_a_call_without_a_verified_project_is_refused_before_any_write(
    repo: AsyncMock, as_project: Any, principal: str | None, project: str | None
) -> None:
    as_project(principal, project)
    with pytest.raises(ProgrammeError) as refused:
        await ProgrammeService(repo).upsert_programme(key="move", title="Move")
    assert refused.value.code == "caller_unidentified"
    repo.upsert_programme.assert_not_awaited()


async def test_reads_need_no_identity(repo: AsyncMock, as_project: Any) -> None:
    as_project(None, None)
    await ProgrammeService(repo).list_programmes(include_done=True)
    repo.list_programmes.assert_awaited_once_with(include_done=True, key=None)


@pytest.mark.parametrize(
    ("call", "code"),
    [
        (lambda s: s.upsert_programme(key="bad key", title="t"), "invalid_key"),
        (lambda s: s.upsert_programme(key="move", title="two\nlines"), "invalid_text"),
        (lambda s: s.upsert_programme(key="move", title="t", state="finished"), "invalid_state"),
        (lambda s: s.upsert_programme(key="move", title="t", position=-1), "invalid_position"),
        (lambda s: s.upsert_gate(programme="move", gate="G 7"), "invalid_key"),
        (lambda s: s.upsert_gate(programme="move", gate="G7", owner="Red_Backup"), "invalid_owner"),
        (lambda s: s.upsert_gate(programme="move", gate="G7", weight=0), "invalid_weight"),
        (lambda s: s.upsert_gate(programme="move", gate="G7", depends_on=["x y"]), "invalid_key"),
        (lambda s: s.upsert_gate(programme="move", gate="G7", state="mostly"), "invalid_state"),
        (lambda s: s.report_gate(programme="move", gate="G7", note="a\u202eb"), "invalid_text"),
        (
            lambda s: s.report_gate(programme="move", gate="G7", note="n", clear_note=True),
            "conflicting_arguments",
        ),
        (
            lambda s: s.report_gate(
                programme="move",
                gate="G7",
                progress={"done": 1, "total": 2, "unit": "t"},
                clear_progress=True,
            ),
            "conflicting_arguments",
        ),
        (
            lambda s: s.report_gate(
                programme="move", gate="G7", progress={"done": 3, "total": 2, "unit": "t"}
            ),
            "invalid_progress",
        ),
        (lambda s: s.report_gate(programme="move", gate="G7"), "nothing_to_report"),
        (lambda s: s.request(programme="move", gate="G7", ask=" padded"), "invalid_text"),
    ],
)
async def test_invalid_input_is_refused_with_its_code_before_the_repository(
    repo: AsyncMock, as_project: Any, call: Any, code: str
) -> None:
    as_project("workstation-claude", "brain-v42")
    with pytest.raises(ProgrammeError) as refused:
        await call(ProgrammeService(repo))
    assert refused.value.code == code
    assert not any(getattr(repo, name).await_count for name in ("upsert_programme", "upsert_gate"))
    assert not any(getattr(repo, name).await_count for name in ("report_gate", "request"))


async def test_valid_writes_reach_the_repository_unchanged(
    repo: AsyncMock, as_project: Any
) -> None:
    as_project("workstation-claude", "brain-v42")
    service = ProgrammeService(repo)
    await service.upsert_gate(
        programme="brain-move-red-base",
        gate="G7",
        title="red-backup deployed on red-base",
        owner="red-backup",
        depends_on=["G6"],
        weight=4,
    )
    assert repo.upsert_gate.await_args.kwargs == {
        "caller": "brain-v42",
        "programme": "brain-move-red-base",
        "gate": "G7",
        "title": "red-backup deployed on red-base",
        "owner": "red-backup",
        "depends_on": ["G6"],
        "weight": 4,
        "position": None,
        "state": None,
    }
    await service.request(programme="brain-move-red-base", gate="G7", ask=None)
    assert repo.request.await_args.kwargs["ask"] is None
