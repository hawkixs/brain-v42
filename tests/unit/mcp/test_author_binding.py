"""Measure prospective author binding without changing a caller's outcome."""

import contextlib
from collections.abc import Iterator
from concurrent.futures import ThreadPoolExecutor
from importlib import import_module, util
from types import ModuleType
from unittest.mock import MagicMock

import pytest


@pytest.fixture
def observer(monkeypatch: pytest.MonkeyPatch) -> Iterator[ModuleType]:
    name = "brain_v42.mcp.author_binding"
    assert util.find_spec(name) is not None, "author binding observation is missing"
    module = import_module(name)
    module.reset_author_binding_counts()
    monkeypatch.setattr(module, "_logger", MagicMock())
    yield module
    module.reset_author_binding_counts()


@pytest.mark.parametrize(
    ("principal", "resolved", "declared", "cause"),
    [
        ("client", "brain-v42", "brain-v42", None),
        ("client", "brain-v42", "other-project", "mismatch"),
        ("client", None, "brain-v42", "unresolved_actor"),
        (None, None, "brain-v42", "unauthenticated"),
        (None, "brain-v42", "brain-v42", "unauthenticated"),
        ("client", "brain-v42", None, None),
    ],
)
def test_observes_only_candidates(
    observer: ModuleType,
    monkeypatch: pytest.MonkeyPatch,
    principal: str | None,
    resolved: str | None,
    declared: str | None,
    cause: str | None,
) -> None:
    monkeypatch.setattr(observer, "get_current_principal", lambda: principal)
    monkeypatch.setattr(observer, "get_current_actor_project", lambda: resolved)
    assert observer.observe_declared_project("brain_ticket_reply", declared) is None
    assert observer.author_binding_counts() == (
        {} if cause is None else {f"brain_ticket_reply:{cause}": 1}
    )
    if cause is None:
        observer._logger.info.assert_not_called()
        observer._logger.debug.assert_not_called()
    else:
        # Every shared-token or stdio write is unauthenticated: logged at DEBUG only.
        level = "debug" if cause == "unauthenticated" else "info"
        other = "info" if level == "debug" else "debug"
        getattr(observer._logger, other).assert_not_called()
        getattr(observer._logger, level).assert_called_once_with(
            "author_binding.candidate",
            tool="brain_ticket_reply",
            cause=cause,
            declared_project=declared,
            actor_project=resolved,
            client_id=principal,
        )


def test_counter_is_thread_safe_and_snapshots_are_independent(
    observer: ModuleType, monkeypatch: pytest.MonkeyPatch
) -> None:
    monkeypatch.setattr(observer, "get_current_principal", lambda: None)
    monkeypatch.setattr(observer, "get_current_actor_project", lambda: None)
    with ThreadPoolExecutor(max_workers=8) as pool:
        list(
            pool.map(
                lambda _: observer.observe_declared_project("brain_ticket_reply", "p"), range(1000)
            )
        )
    snapshot = observer.author_binding_counts()
    assert snapshot == {"brain_ticket_reply:unauthenticated": 1000}
    snapshot.clear()
    assert observer.author_binding_counts() == {"brain_ticket_reply:unauthenticated": 1000}
    observer.reset_author_binding_counts()
    assert observer.author_binding_counts() == {}


@pytest.mark.parametrize("read", ["get_current_principal", "get_current_actor_project"])
def test_provenance_failure_is_swallowed_and_logged_by_type_only(
    observer: ModuleType, monkeypatch: pytest.MonkeyPatch, read: str
) -> None:
    monkeypatch.setattr(observer, read, MagicMock(side_effect=RuntimeError("private detail")))
    assert observer.observe_declared_project("brain_ticket_reply", "project") is None
    assert observer.author_binding_counts() == {}
    observer._logger.warning.assert_called_once_with(
        "author_binding.failed", error_type="RuntimeError"
    )


def test_log_labels_are_bounded_and_sanitized(
    observer: ModuleType, monkeypatch: pytest.MonkeyPatch
) -> None:
    monkeypatch.setattr(observer, "get_current_principal", lambda: "c\n" + "x" * 1000)
    monkeypatch.setattr(observer, "get_current_actor_project", lambda: "a\n" + "x" * 1000)
    observer.observe_declared_project("brain_ticket_reply", "d\n" + "x" * 1000)
    fields = observer._logger.info.call_args.kwargs
    for name in ("declared_project", "actor_project", "client_id", "tool"):
        assert len(fields[name]) <= 64
        assert "\n" not in fields[name]


@pytest.mark.parametrize(
    ("principal", "actor", "cause", "level"),
    [
        (None, None, "unauthenticated", "debug"),
        ("client-a", None, "unresolved_actor", "info"),
        ("client-a", "project-a", "mismatch", "info"),
    ],
)
def test_logging_failure_cannot_interrupt_the_write(
    observer: ModuleType,
    monkeypatch: pytest.MonkeyPatch,
    principal: str | None,
    actor: str | None,
    cause: str,
    level: str,
) -> None:
    monkeypatch.setattr(observer, "get_current_principal", lambda: principal)
    monkeypatch.setattr(observer, "get_current_actor_project", lambda: actor)
    for name in ("debug", "info", "warning"):
        getattr(observer._logger, name).side_effect = RuntimeError("private detail")
    assert observer.observe_declared_project("brain_ticket_reply", "project") is None
    # The failing call is the one this cause logs with, so the guard is exercised.
    getattr(observer._logger, level).assert_called_once()
    observer._logger.warning.assert_called_once_with(
        "author_binding.failed", error_type="RuntimeError"
    )
    assert observer.author_binding_counts() == {f"brain_ticket_reply:{cause}": 1}


async def test_a_real_tool_call_reads_the_real_provenance_context(observer: ModuleType) -> None:
    """Wire the observer to the ContextVars the middleware sets, through a real tool."""
    from unittest.mock import AsyncMock  # noqa: PLC0415

    from fastmcp import FastMCP  # noqa: PLC0415

    from brain_v42.mcp.tools import ticket_tools  # noqa: PLC0415
    from brain_v42.provenance import (  # noqa: PLC0415
        set_current_actor_project,
        set_current_principal,
    )

    service = MagicMock()
    service.create = AsyncMock(side_effect=RuntimeError("service not under test"))
    app = FastMCP("author-binding-wiring")
    ticket_tools.register_ticket_tools(app, service)
    tool = await app.get_tool("brain_ticket_create")
    assert tool is not None
    set_current_principal("client-a")
    set_current_actor_project("project-a")
    try:
        with contextlib.suppress(Exception):
            await tool.fn(
                from_project="project-b",
                to_project="project-a",
                kind="request",
                title="t",
                body="b",
            )
    finally:
        set_current_principal(None)
        set_current_actor_project(None)
    assert observer.author_binding_counts() == {"brain_ticket_create:mismatch": 1}
