"""RED unit contract for BrainSessionService with a mocked repository."""

from __future__ import annotations

import importlib
from types import SimpleNamespace
from typing import Any
from unittest.mock import AsyncMock, MagicMock
from uuid import uuid4

import pytest


def _service_symbol(name: str) -> Any:
    """Resolve lazily so an incomplete module still collects successfully."""
    module = importlib.import_module("brain_v42.services.brain_session_service")
    value = getattr(module, name, None)
    assert value is not None, f"brain_session_service must define {name}"
    return value


def _repo() -> MagicMock:
    repo = MagicMock()
    repo.start = AsyncMock()
    repo.resume = AsyncMock()
    repo.capture = AsyncMock()
    repo.heartbeat = AsyncMock()
    repo.end = AsyncMock()
    repo.list = AsyncMock()
    repo.abandon = AsyncMock()
    return repo


def _service(repo: MagicMock) -> Any:
    return _service_symbol("BrainSessionService")(repo=repo)


def _result(**values: Any) -> SimpleNamespace:
    return SimpleNamespace(**values)


def test_dedicated_error_hierarchy_is_public() -> None:
    base = _service_symbol("BrainSessionError")
    input_error = _service_symbol("BrainSessionInputError")
    not_found = _service_symbol("BrainSessionNotFoundError")
    state = _service_symbol("BrainSessionStateError")
    conflict = _service_symbol("BrainSessionConflictError")
    client_conflict = _service_symbol("BrainSessionClientKeyConflictError")
    focus_conflict = _service_symbol("BrainSessionFocusConflictError")
    terminal_conflict = _service_symbol("BrainSessionTerminalConflictError")

    assert issubclass(input_error, base)
    assert issubclass(not_found, base)
    assert issubclass(state, base)
    assert issubclass(conflict, base)
    assert issubclass(client_conflict, conflict)
    assert issubclass(focus_conflict, conflict)
    assert issubclass(terminal_conflict, conflict)


def test_service_is_exported_from_services_package() -> None:
    import brain_v42.services as services

    assert services.BrainSessionService is _service_symbol("BrainSessionService")
    assert "BrainSessionService" in services.__all__


async def test_start_canonicalizes_strict_project_key_and_trims_client_key() -> None:
    repo = _repo()
    expected = _result(session_id=uuid4(), open_session_count=2)
    repo.start.return_value = expected

    actual = await _service(repo).start(" brain_v42 ", " codex-task-42 ")

    repo.start.assert_awaited_once_with("brain-v42", "codex-task-42")
    assert actual is expected


@pytest.mark.parametrize("client_key", ["", "   ", "x" * 129])
async def test_start_rejects_invalid_client_key_before_repository(client_key: str) -> None:
    repo = _repo()
    error = _service_symbol("BrainSessionInputError")

    with pytest.raises(error):
        await _service(repo).start("brain-v42", client_key)

    repo.start.assert_not_awaited()


async def test_start_translates_noncanonical_project_key_to_input_error() -> None:
    repo = _repo()
    error = _service_symbol("BrainSessionInputError")

    with pytest.raises(error, match="project_key"):
        await _service(repo).start("RED_DATA", "client-1")

    repo.start.assert_not_awaited()


async def test_start_propagates_client_key_conflict() -> None:
    repo = _repo()
    error = _service_symbol("BrainSessionClientKeyConflictError")
    repo.start.side_effect = error("client key conflict")

    with pytest.raises(error):
        await _service(repo).start("brain-v42", "client-1")


async def test_resume_delegates_and_returns_structured_result() -> None:
    repo = _repo()
    session_id = uuid4()
    expected = _result(session=_result(id=session_id, status="open"))
    repo.resume.return_value = expected

    actual = await _service(repo).resume(session_id, " task-a ")

    repo.resume.assert_awaited_once_with(session_id, "task-a")
    assert actual is expected


@pytest.mark.parametrize(
    "error_name",
    ["BrainSessionNotFoundError", "BrainSessionStateError"],
)
async def test_resume_rejects_missing_or_non_open_session(error_name: str) -> None:
    repo = _repo()
    error = _service_symbol(error_name)
    repo.resume.side_effect = error("cannot resume")

    with pytest.raises(error):
        await _service(repo).resume(uuid4(), "task-a")


def _valid_end_kwargs() -> dict[str, Any]:
    return {
        "session_id": uuid4(),
        "expected_client_key": "task-a",
        "summary": "Lifecycle contract completed",
        "next_focus": "Implement MCP lifecycle tools",
        "expected_focus_revision": 7,
        "nothing_to_capture_reason": None,
    }


@pytest.mark.parametrize(
    ("field", "value"),
    [
        ("summary", ""),
        ("summary", "   "),
        ("next_focus", ""),
        ("next_focus", "   "),
        ("expected_focus_revision", -1),
    ],
)
async def test_end_rejects_invalid_required_input(field: str, value: Any) -> None:
    repo = _repo()
    kwargs = _valid_end_kwargs()
    kwargs[field] = value
    error = _service_symbol("BrainSessionInputError")

    with pytest.raises(error):
        await _service(repo).end(**kwargs)

    repo.end.assert_not_awaited()


async def test_end_delegates_normalized_payload() -> None:
    repo = _repo()
    expected = _result(replayed=False, current_focus_revision=8)
    repo.end.return_value = expected
    session_id = uuid4()

    actual = await _service(repo).end(
        session_id,
        " task-a ",
        "  Lifecycle completed  ",
        "  Implement MCP tools  ",
        7,
    )

    repo.end.assert_awaited_once_with(
        session_id,
        "task-a",
        "Lifecycle completed",
        "Implement MCP tools",
        7,
        nothing_to_capture_reason=None,
    )
    assert actual is expected


async def test_end_accepts_explicit_nothing_to_capture_reason() -> None:
    repo = _repo()
    expected = _result(replayed=False)
    repo.end.return_value = expected
    session_id = uuid4()

    actual = await _service(repo).end(
        session_id,
        " task-a ",
        "  Investigation only  ",
        "  Continue B3  ",
        4,
        nothing_to_capture_reason="  No durable finding  ",
    )

    repo.end.assert_awaited_once_with(
        session_id,
        "task-a",
        "Investigation only",
        "Continue B3",
        4,
        nothing_to_capture_reason="No durable finding",
    )
    assert actual is expected


@pytest.mark.parametrize(
    "error_name",
    [
        "BrainSessionNotFoundError",
        "BrainSessionStateError",
        "BrainSessionFocusConflictError",
        "BrainSessionTerminalConflictError",
    ],
)
async def test_end_propagates_repository_lifecycle_errors(error_name: str) -> None:
    repo = _repo()
    error = _service_symbol(error_name)
    repo.end.side_effect = error("end failed")

    with pytest.raises(error):
        await _service(repo).end(**_valid_end_kwargs())


async def test_list_canonicalizes_project_and_uses_open_default() -> None:
    repo = _repo()
    expected = _result(sessions=[], total=0, limit=20, offset=0)
    repo.list.return_value = expected

    actual = await _service(repo).list(project_key="brain_v42")

    repo.list.assert_awaited_once_with(project_key="brain-v42", status="open", limit=20, offset=0)
    assert actual is expected


async def test_list_accepts_all_statuses_filter() -> None:
    repo = _repo()
    expected = _result(sessions=[], total=0, limit=20, offset=0)
    repo.list.return_value = expected

    actual = await _service(repo).list(status="all")

    repo.list.assert_awaited_once_with(project_key=None, status="all", limit=20, offset=0)
    assert actual is expected


@pytest.mark.parametrize("limit", [0, 101])
async def test_list_rejects_limit_outside_bounded_range(limit: int) -> None:
    repo = _repo()
    error = _service_symbol("BrainSessionInputError")

    with pytest.raises(error, match="limit"):
        await _service(repo).list(limit=limit)

    repo.list.assert_not_awaited()


async def test_abandon_requires_nonblank_reason() -> None:
    repo = _repo()
    error = _service_symbol("BrainSessionInputError")

    with pytest.raises(error):
        await _service(repo).abandon(uuid4(), "task-a", "   ")

    repo.abandon.assert_not_awaited()


async def test_abandon_delegates_trimmed_reason() -> None:
    repo = _repo()
    session_id = uuid4()
    expected = _result(replayed=False, remaining_open_session_count=0)
    repo.abandon.return_value = expected

    actual = await _service(repo).abandon(
        session_id,
        " task-a ",
        "  superseded task  ",
    )

    repo.abandon.assert_awaited_once_with(session_id, "task-a", "superseded task")
    assert actual is expected


@pytest.mark.parametrize(
    "error_name",
    ["BrainSessionNotFoundError", "BrainSessionTerminalConflictError"],
)
async def test_abandon_propagates_repository_errors(error_name: str) -> None:
    repo = _repo()
    error = _service_symbol(error_name)
    repo.abandon.side_effect = error("abandon failed")

    with pytest.raises(error):
        await _service(repo).abandon(uuid4(), "task-a", "superseded")
