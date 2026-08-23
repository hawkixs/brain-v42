"""RED contract tests for the explicit Brain session lifecycle MCP tools."""

from __future__ import annotations

import importlib
from types import ModuleType
from typing import Any
from unittest.mock import AsyncMock, MagicMock
from uuid import uuid4

import pytest
from fastmcp import FastMCP
from mcp.types import ToolAnnotations
from pydantic import ValidationError

LIFECYCLE_MODULE = "brain_v42.mcp.tools.session_lifecycle_tools"
MODEL_MODULE = "brain_v42.models.brain_session"
TOOL_NAMES = (
    "brain_session_start",
    "brain_session_capture",
    "brain_session_heartbeat",
    "brain_session_end",
    "brain_session_list",
    "brain_session_resume",
    "brain_session_abandon",
)


def _module(name: str) -> ModuleType:
    """Import lazily so a missing lifecycle module is a clear test failure."""
    try:
        return importlib.import_module(name)
    except ModuleNotFoundError as exc:
        assert exc.name != name, f"required module {name} is not implemented"
        raise


def _symbol(module_name: str, name: str) -> Any:
    value = getattr(_module(module_name), name, None)
    assert value is not None, f"{module_name} must define {name}"
    return value


def _registered_server() -> tuple[FastMCP, MagicMock, AsyncMock]:
    registrar = _symbol(LIFECYCLE_MODULE, "register_session_lifecycle_tools")
    service = MagicMock()
    briefing_loader = AsyncMock()
    server = FastMCP("brain-session-lifecycle-test")
    registrar(server, service, briefing_loader)
    return server, service, briefing_loader


def _result(name: str, **values: Any) -> Any:
    result_type = _symbol(MODEL_MODULE, name)
    assert callable(getattr(result_type, "model_construct", None)), (
        f"{name} must be a structured Pydantic result"
    )
    return result_type.model_construct(**values)


async def _tool(server: FastMCP, name: str) -> Any:
    tool = await server.get_tool(name)
    assert tool is not None, f"missing MCP tool {name}"
    return tool


async def test_registers_all_seven_explicit_lifecycle_tools() -> None:
    server, _, _ = _registered_server()

    registered = {name for name in TOOL_NAMES if await server.get_tool(name) is not None}

    assert registered == set(TOOL_NAMES)


async def test_lifecycle_tools_publish_exact_safety_annotations() -> None:
    server, _, _ = _registered_server()
    expected = {
        "brain_session_start": ToolAnnotations(
            readOnlyHint=False,
            destructiveHint=False,
            idempotentHint=True,
            openWorldHint=False,
        ),
        "brain_session_capture": ToolAnnotations(
            readOnlyHint=False,
            destructiveHint=False,
            idempotentHint=True,
            openWorldHint=False,
        ),
        "brain_session_heartbeat": ToolAnnotations(
            readOnlyHint=False,
            destructiveHint=False,
            idempotentHint=False,
            openWorldHint=False,
        ),
        "brain_session_end": ToolAnnotations(
            readOnlyHint=False,
            destructiveHint=True,
            idempotentHint=True,
            openWorldHint=False,
        ),
        "brain_session_list": ToolAnnotations(
            readOnlyHint=True,
            destructiveHint=False,
            idempotentHint=True,
            openWorldHint=False,
        ),
        "brain_session_resume": ToolAnnotations(
            readOnlyHint=True,
            destructiveHint=False,
            idempotentHint=True,
            openWorldHint=False,
        ),
        "brain_session_abandon": ToolAnnotations(
            readOnlyHint=False,
            destructiveHint=True,
            idempotentHint=True,
            openWorldHint=False,
        ),
    }

    for name, expected_annotations in expected.items():
        tool = await _tool(server, name)
        assert tool.annotations == expected_annotations, name


async def test_lifecycle_docstrings_keep_boundaries_under_user_control() -> None:
    """Le covenant tel que le CLIENT le reçoit, pas tel que le source l'écrit.

    Second point d'ancrage, et il n'est pas redondant avec
    `test_session_covenant_docstrings_anchor` : celui-là lit l'AST du module,
    celui-ci lit la `description` que FastMCP publie réellement. Les deux ont
    rougi ensemble à la réécriture par nature (046) — le premier était nommé
    dans le mandat, le second ne l'était pas. C'est la classe d'instantané
    périmé « document SATELLITE » : seule une suite complète le sort.

    Réécrit par nature (ADR §0ter (d)) : depuis la 046 le serveur ouvre et ferme
    ses propres traçantes `agent`. Le covenant NOMME cette exception au lieu de
    la nier ; la phrase d'avant est retenue ci-dessous comme témoin négatif.
    """
    server, _, _ = _registered_server()

    for name in TOOL_NAMES:
        description = " ".join((await _tool(server, name)).description.lower().split())
        assert "explicit user command" in description, name
        assert "no hook and no auto-close may invoke this lifecycle boundary" in description, name
        assert "an agent tracer is the only session the server opens or closes" in description, name
        assert "no hook or auto-close" not in description, name


async def test_lifecycle_input_schemas_are_bounded_and_discoverable() -> None:
    server, _, _ = _registered_server()

    start = (await _tool(server, "brain_session_start")).parameters["properties"]
    capture = (await _tool(server, "brain_session_capture")).parameters["properties"]
    end = (await _tool(server, "brain_session_end")).parameters["properties"]
    listing = (await _tool(server, "brain_session_list")).parameters["properties"]
    abandon = (await _tool(server, "brain_session_abandon")).parameters["properties"]

    assert start["project_key"]["minLength"] == 1
    assert start["project_key"]["maxLength"] == 50
    assert start["client_key"]["minLength"] == 1
    assert start["client_key"]["maxLength"] == 128
    assert "retry" in start["client_key"]["description"].lower()
    assert "parallel" in start["client_key"]["description"].lower()
    assert end["summary"]["minLength"] == 1
    assert end["next_focus"]["minLength"] == 1
    assert end["expected_focus_revision"]["minimum"] == 0
    list_project_key = next(
        variant for variant in listing["project_key"]["anyOf"] if variant.get("type") == "string"
    )
    assert list_project_key["minLength"] == 1
    assert list_project_key["maxLength"] == 50
    # `closed_inactive` ajouté par `24ca3b73` : le 4e état de la 046 était
    # atteignable en base et posé par le balayage, mais absent du seul filtre
    # publié — donc indemandable par un client. Coût MESURÉ du seul ajout à
    # cette énumération : +18 octets sur le schéma d'entrée de ce tool (377 ->
    # 395), et zéro sur les sept schémas de sortie.
    assert listing["status"]["enum"] == [
        "open",
        "stale",
        "ended",
        "abandoned",
        "closed_inactive",
        "all",
    ]
    assert listing["limit"]["minimum"] == 1
    assert listing["limit"]["maximum"] == 100
    assert listing["offset"]["minimum"] == 0
    assert abandon["reason"]["minLength"] == 1
    assert capture["knowledge_ids"]["minItems"] == 1
    assert capture["knowledge_ids"]["maxItems"] == 100
    for schema in (capture, end, abandon):
        assert schema["expected_client_key"]["maxLength"] == 128


async def test_next_focus_description_scopes_judgment_out_of_measurable_state() -> None:
    server, _, _ = _registered_server()
    end = (await _tool(server, "brain_session_end")).parameters["properties"]

    description = end["next_focus"]["description"].lower()

    assert "jugement" in description
    assert "briefing" in description
    assert "mesur" in description  # couvre mesurable/mesuré/mesure


async def test_end_rejects_boolean_focus_revision_before_service_call() -> None:
    server, service, _ = _registered_server()
    tool = await _tool(server, "brain_session_end")

    with pytest.raises(ValidationError):
        await tool.run(
            {
                "session_id": str(uuid4()),
                "expected_client_key": "task-a",
                "summary": "done",
                "next_focus": "next",
                "expected_focus_revision": True,
                "nothing_to_capture_reason": "nothing durable",
            }
        )

    service.end.assert_not_called()


async def test_capture_list_schema_is_bounded_to_one_hundred_ids() -> None:
    server, _, _ = _registered_server()
    capture_array = (await _tool(server, "brain_session_capture")).parameters["properties"][
        "knowledge_ids"
    ]

    assert capture_array["minItems"] == 1
    assert capture_array["maxItems"] == 100


async def test_start_forwards_identity_and_adds_briefing_to_structured_result() -> None:
    server, service, briefing_loader = _registered_server()
    session = MagicMock(project_key="brain-v42")
    service_result = _result(
        "BrainSessionStartResult",
        session=session,
        replayed=False,
        open_session_count=2,
        briefing="",
    )
    service.start = AsyncMock(return_value=service_result)
    briefing_loader.return_value = "## Brain briefing"

    tool = await _tool(server, "brain_session_start")
    result = await tool.fn(project_key="brain-v42", client_key="codex-task-42")

    service.start.assert_awaited_once_with(project_key="brain-v42", client_key="codex-task-42")
    briefing_loader.assert_awaited_once_with("brain-v42")
    assert tool.version == "4.0"
    assert isinstance(result, _symbol(MODEL_MODULE, "BrainSessionStartResult"))
    assert result is not service_result
    assert result.briefing == "## Brain briefing"
    assert service_result.briefing == ""


async def test_start_returns_persisted_session_when_total_briefing_load_fails() -> None:
    server, service, briefing_loader = _registered_server()
    session = MagicMock(project_key="brain-v42", id=uuid4())
    service_result = _result(
        "BrainSessionStartResult",
        session=session,
        replayed=False,
        open_session_count=1,
        briefing="",
    )
    service.start = AsyncMock(return_value=service_result)
    briefing_loader.side_effect = RuntimeError("briefing backend unavailable")

    result = await (await _tool(server, "brain_session_start")).fn(
        project_key="brain-v42",
        client_key="codex-task-briefing-failure",
    )

    assert result.session is session
    assert result.replayed is False
    assert "briefing unavailable" in result.briefing.lower()


async def test_resume_forwards_session_id_and_adds_its_project_briefing() -> None:
    server, service, briefing_loader = _registered_server()
    session_id = uuid4()
    session = MagicMock(project_key="brain-v42")
    service_result = _result(
        "BrainSessionResumeResult",
        session=session,
        open_session_count=2,
        current_focus="Implement lifecycle",
        current_focus_revision=8,
        briefing="",
    )
    service.resume = AsyncMock(return_value=service_result)
    briefing_loader.return_value = "## Resumed briefing"

    result = await (await _tool(server, "brain_session_resume")).fn(
        session_id=session_id,
        expected_client_key="task-a",
    )

    service.resume.assert_awaited_once_with(
        session_id=session_id,
        expected_client_key="task-a",
    )
    briefing_loader.assert_awaited_once_with("brain-v42")
    assert isinstance(result, _symbol(MODEL_MODULE, "BrainSessionResumeResult"))
    assert result is not service_result
    assert result.briefing == "## Resumed briefing"
    assert service_result.briefing == ""


async def test_end_forwards_fail_closed_completion_parameters() -> None:
    server, service, _ = _registered_server()
    session_id = uuid4()
    service_result = _result("BrainSessionEndResult", session=MagicMock())
    service.end = AsyncMock(return_value=service_result)

    result = await (await _tool(server, "brain_session_end")).fn(
        session_id=session_id,
        expected_client_key="task-a",
        summary="Lifecycle implemented",
        next_focus="Run integration tests",
        expected_focus_revision=8,
        nothing_to_capture_reason="No durable knowledge produced",
    )

    service.end.assert_awaited_once_with(
        session_id=session_id,
        expected_client_key="task-a",
        summary="Lifecycle implemented",
        next_focus="Run integration tests",
        expected_focus_revision=8,
        nothing_to_capture_reason="No durable knowledge produced",
    )
    assert result is service_result


async def test_list_forwards_filters_and_pagination() -> None:
    server, service, _ = _registered_server()
    service_result = _result("BrainSessionListResult", sessions=[], total=0, limit=5, offset=10)
    service.list = AsyncMock(return_value=service_result)

    result = await (await _tool(server, "brain_session_list")).fn(
        project_key="brain-v42", status="open", limit=5, offset=10
    )

    service.list.assert_awaited_once_with(
        project_key="brain-v42", status="open", limit=5, offset=10
    )
    assert result is service_result


async def test_abandon_forwards_session_id_and_explicit_reason() -> None:
    server, service, _ = _registered_server()
    session_id = uuid4()
    service_result = _result("BrainSessionAbandonResult", session=MagicMock())
    service.abandon = AsyncMock(return_value=service_result)

    result = await (await _tool(server, "brain_session_abandon")).fn(
        session_id=session_id,
        expected_client_key="task-a",
        reason="Task superseded by user",
    )

    service.abandon.assert_awaited_once_with(
        session_id=session_id,
        expected_client_key="task-a",
        reason="Task superseded by user",
    )
    assert result is service_result


async def test_capture_and_heartbeat_forward_identity_guard() -> None:
    server, service, _ = _registered_server()
    session_id = uuid4()
    knowledge_id = uuid4()
    capture_result = _result("BrainSessionCaptureResult", session=MagicMock())
    heartbeat_result = _result("BrainSessionHeartbeatResult", session=MagicMock())
    service.capture = AsyncMock(return_value=capture_result)
    service.heartbeat = AsyncMock(return_value=heartbeat_result)

    captured = await (await _tool(server, "brain_session_capture")).fn(
        session_id=session_id,
        expected_client_key="task-a",
        knowledge_ids=[knowledge_id],
    )
    heartbeat = await (await _tool(server, "brain_session_heartbeat")).fn(
        session_id=session_id,
        expected_client_key="task-a",
    )

    service.capture.assert_awaited_once_with(
        session_id=session_id,
        expected_client_key="task-a",
        knowledge_ids=[knowledge_id],
    )
    service.heartbeat.assert_awaited_once_with(
        session_id=session_id,
        expected_client_key="task-a",
    )
    assert captured is capture_result
    assert heartbeat is heartbeat_result
