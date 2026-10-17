"""The scope census is explicit policy, independent of parameter vocabulary."""

import inspect
from unittest.mock import MagicMock

import pytest

from brain_v42.mcp import server
from brain_v42.mcp.client_principal import ClientPrincipal
from brain_v42.mcp.project_scope import (
    ACTOR_REQUIRED,
    DEFERRED_TO_PR4,
    SCOPE_ARGUMENTS,
    SCOPE_EXEMPT,
    scope_decision,
)
from brain_v42.mcp.tool_families import NEUTRAL_TOOLS, TOOL_FAMILIES


def principal(projects: tuple[str, ...] | None = ("brain-v42",)) -> ClientPrincipal:
    return ClientPrincipal("coordination", frozenset({"read", "write", "delivery"}), (), projects)


def test_every_write_and_delivery_tool_has_exactly_one_scope_classification() -> None:
    direct = set(SCOPE_ARGUMENTS) | ACTOR_REQUIRED
    categories = (direct, DEFERRED_TO_PR4, SCOPE_EXEMPT)
    writes = {name for name, family in TOOL_FAMILIES.items() if family in {"write", "delivery"}}
    assert set.union(*(set(category) for category in categories)) == writes
    for name in writes:
        assert sum(name in category for category in categories) == 1, name


async def test_allowlisted_parameters_exist_in_registered_tool_schemas() -> None:
    from brain_v42.mcp.tools.brain_tools import register_tools
    from brain_v42.mcp.tools.claim_tools import register_claim_tools
    from brain_v42.mcp.tools.crud_tools import register_crud_tools
    from brain_v42.mcp.tools.decay_tools import register_decay_tools
    from brain_v42.mcp.tools.delivery_tools import register_delivery_tools
    from brain_v42.mcp.tools.focus_slot_tools import register_focus_slot_tools
    from brain_v42.mcp.tools.programme_tools import register_programme_tools
    from brain_v42.mcp.tools.session_tools import register_session_tools
    from brain_v42.mcp.tools.ticket_tools import register_ticket_tools

    mcp = server.create_mcp_instance()
    for register in (
        register_tools,
        register_claim_tools,
        register_crud_tools,
        register_decay_tools,
        register_delivery_tools,
        register_focus_slot_tools,
        register_programme_tools,
        register_session_tools,
        register_ticket_tools,
    ):
        required = {
            name: MagicMock()
            for name, parameter in inspect.signature(register).parameters.items()
            if name != "mcp" and parameter.default is inspect.Parameter.empty
        }
        register(mcp, **required)
    tools = {tool.name: tool for tool in await mcp._list_tools()}
    for name, arguments in SCOPE_ARGUMENTS.items():
        assert name in tools
        assert set(arguments) <= tools[name].parameters["properties"].keys(), name
    registered_writes = {name for name in tools if TOOL_FAMILIES.get(name) in {"write", "delivery"}}
    assert (
        registered_writes == set(SCOPE_ARGUMENTS) | ACTOR_REQUIRED | DEFERRED_TO_PR4 | SCOPE_EXEMPT
    )


def test_ticket_destination_is_never_consulted() -> None:
    assert (
        scope_decision(
            "brain_ticket_create",
            {"from_project": "brain-v42", "to_project": "outside"},
            principal(),
            "brain-v42",
        )
        is None
    )


@pytest.mark.parametrize("tool,arguments", SCOPE_ARGUMENTS.items())
def test_each_direct_project_argument_must_be_in_scope(
    tool: str, arguments: tuple[str, ...]
) -> None:
    assert (
        scope_decision(tool, dict.fromkeys(arguments, "brain-v42"), principal(), "brain-v42")
        is None
    )
    decision = scope_decision(tool, dict.fromkeys(arguments, "outside"), principal(), "brain-v42")
    assert decision is not None
    assert (decision.cause, decision.reason) == ("scope_mismatch", "scope_denied")


@pytest.mark.parametrize("key", ["brain", "brain_v42", " brain-v42 "])
def test_project_aliases_and_whitespace_are_canonicalized(key: str) -> None:
    assert scope_decision("brain_learn", {"project_key": key}, principal(), None) is None


@pytest.mark.parametrize("key", ["red-shrik", "red-shrik:agent-a:child"])
def test_colon_scope_has_no_inheritance(key: str) -> None:
    decision = scope_decision(
        "brain_learn", {"project_key": key}, principal(("red-shrik:agent-a",)), None
    )
    assert decision is not None
    assert (decision.cause, decision.reason) == ("scope_mismatch", "scope_denied")


@pytest.mark.parametrize("target,actor", [("brain-v42", "outside"), ("outside", "brain-v42")])
def test_actor_and_target_are_checked_independently(target: str, actor: str) -> None:
    decision = scope_decision("brain_learn", {"project_key": target}, principal(), actor)
    assert decision is not None
    assert (decision.cause, decision.reason) == ("scope_mismatch", "scope_denied")


@pytest.mark.parametrize("tool,arguments", SCOPE_ARGUMENTS.items())
@pytest.mark.parametrize("missing", [True, False])
def test_every_required_target_refuses_null_even_with_an_in_scope_actor(
    tool: str, arguments: tuple[str, ...], missing: bool
) -> None:
    if tool == "brain_programme_gate_upsert":
        return  # Its owner is deliberately optional.
    supplied = {} if missing else dict.fromkeys(arguments)
    decision = scope_decision(tool, supplied, principal(), "brain-v42")
    assert decision is not None
    assert (decision.cause, decision.reason) == ("scope_unresolved", "null_project_refused")


@pytest.mark.parametrize("tool,arguments", SCOPE_ARGUMENTS.items())
@pytest.mark.parametrize("value", [12, "Bad key", ""])
def test_invalid_declared_projects_are_not_wildcards(
    tool: str, arguments: tuple[str, ...], value: object
) -> None:
    decision = scope_decision(tool, dict.fromkeys(arguments, value), principal(), "brain-v42")
    assert decision is not None
    assert decision.cause == "scope_unresolved"
    assert decision.reason == (
        "null_project_refused"
        if not isinstance(value, str) and tool != "brain_programme_gate_upsert"
        else "scope_denied"
    )


@pytest.mark.parametrize("tool", sorted(ACTOR_REQUIRED))
def test_actor_only_writes_require_a_resolved_actor(tool: str) -> None:
    decision = scope_decision(tool, {}, principal(), None)
    assert decision is not None
    assert (decision.cause, decision.reason) == ("scope_unresolved", "null_project_refused")


@pytest.mark.parametrize("owner", [None, "brain-v42", "outside"])
def test_gate_owner_is_optional_and_independent(owner: str | None) -> None:
    decision = scope_decision(
        "brain_programme_gate_upsert", {"owner": owner}, principal(), "brain-v42"
    )
    if owner == "outside":
        assert decision is not None and decision.cause == "scope_mismatch"
    else:
        assert decision is None
    assert scope_decision("brain_programme_gate_upsert", {}, principal(), "brain-v42") is None


@pytest.mark.parametrize("tool", sorted(DEFERRED_TO_PR4))
@pytest.mark.parametrize("actor", [None, "brain-v42", "outside"])
def test_deferred_tools_check_only_a_resolved_actor(tool: str, actor: str | None) -> None:
    decision = scope_decision(tool, {}, principal(), actor)
    if actor == "outside":
        assert decision is not None and decision.reason == "scope_denied"
    else:
        assert decision is None


@pytest.mark.parametrize("tool", sorted(set(TOOL_FAMILIES) | NEUTRAL_TOOLS))
def test_null_scope_principal_has_no_scope_decision(tool: str) -> None:
    assert scope_decision(tool, {}, principal(None), "outside") is None
    assert scope_decision(tool, {}, None, "outside") is None


@pytest.mark.parametrize(
    "tool",
    sorted(
        SCOPE_EXEMPT
        | NEUTRAL_TOOLS
        | {n for n, f in TOOL_FAMILIES.items() if f in {"read", "admin"}}
    ),
)
def test_reads_admin_and_outer_gateway_are_exempt(tool: str) -> None:
    assert scope_decision(tool, {"project_key": "outside"}, principal(), "outside") is None
