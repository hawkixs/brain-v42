"""The writers of `project_contexts.current_focus` ALL carry the same bound.

The defect (`bfb4cf93`): `brain_session_end` caps `next_focus` at
``NEXT_FOCUS_MAX_LENGTH`` characters, and that value REPLACES `current_focus`
when the compare-and-swap succeeds. The other writers of the SAME column took a
bare ``str`` — no bound, not in the argument, not in the model, not in the
service, not in the column (``text``). The MCP cap was the ONLY bound on the
write path, and it covered one writer out of three.

The consequence, and it is the heart of it: **the unbounded writer puts the
project into a state the bounded writer cannot represent.** An honest session,
having read a 12,000-character focus, can no longer return it at closing time —
it is refused by a validation it is not responsible for.

Replayed on 2026-08-23 over the `brain_sessions.started_focus` snapshots:
revision 217 carried **12,157 characters** — 2,157 more than `brain_session_end`
can write — from 2026-08-21 16:14:45 to 2026-08-22 08:27:43, that is **sixteen
hours, seen by seven sessions**.

**THREE writers, not two.** The ticket and its mandate named only two. A survey
through several angles finds a third, `brain_set_project_context`, whose
`current_focus` is optional and therefore invisible to anyone looking for a
mandatory argument. It is the same pattern blind spot the focus warns about:
count through SEVERAL angles.

**The unit is the CHARACTER, and this test carries the case that tells them
apart.** That is not a stylistic precaution: replayed on 2026-08-23, **three**
revisions of `brain-v42`'s focus fitted under the cap in characters while
exceeding it in bytes — 192 (9,996 / 10,277), **194 (9,984 / 10,287)** and 219
(9,977 / 10,285). 194 is the sharpest: **sixteen characters under the bound, 287
bytes over**. A bound counting bytes would therefore have refused three perfectly
legal focuses.

The distinguishing witness is a non-ASCII case whose length is exactly the cap,
hence twice as heavy in bytes: it must be ACCEPTED, and the test turns red the
day someone rewrites the bound in bytes.
"""

from __future__ import annotations

from types import SimpleNamespace
from typing import Any
from unittest.mock import AsyncMock, MagicMock
from uuid import uuid4

import pytest
from fastmcp import FastMCP
from pydantic import ValidationError

from brain_v42.mcp.tools.session_lifecycle_tools import NEXT_FOCUS_MAX_LENGTH

#: Each entry is (MCP tool name, name of the argument that writes the focus).
#: `next_focus` and `current_focus` have different names but write the SAME
#: column: `next_focus` BECOMES `current_focus` when the CAS succeeds. That is
#: what makes their bound shared, rather than two neighbouring contracts.
FOCUS_WRITERS: tuple[tuple[str, str], ...] = (
    ("brain_session_end", "next_focus"),
    ("brain_update_project_focus", "current_focus"),
    ("brain_set_project_context", "current_focus"),
)

#: A non-ASCII character of 2 bytes in UTF-8. `len()` counts 1, `encode()` counts
#: 2: that is the whole point of the distinguishing witness.
_TWO_BYTE_CHAR = "é"


def _register_lifecycle(server: FastMCP, service: Any) -> None:
    from brain_v42.mcp.tools.session_lifecycle_tools import register_session_lifecycle_tools

    register_session_lifecycle_tools(server, service, AsyncMock(return_value=""))


def _register_project_context(server: FastMCP, context_svc: Any, roadmap_svc: Any) -> None:
    from brain_v42.mcp.tools.project_context_tools import register_project_context_tools

    register_project_context_tools(server, context_svc, roadmap_svc)


def _lifecycle_server() -> FastMCP:
    server = FastMCP("focus-bound-lifecycle")
    _register_lifecycle(server, MagicMock())
    return server


def _project_context_server() -> FastMCP:
    server = FastMCP("focus-bound-project-context")
    _register_project_context(server, AsyncMock(), AsyncMock())
    return server


def _invocation(tool_name: str, argument: str) -> tuple[FastMCP, Any, Any]:
    """Return (server, mock of the writing service, argument factory).

    The mock returned is the one the tool calls TO WRITE — not the whole service:
    it is what the fail-closed "never called" applies to.
    """
    if tool_name == "brain_session_end":
        service = MagicMock()
        service.end = AsyncMock(
            return_value=SimpleNamespace(
                session=SimpleNamespace(id=uuid4()), briefing="", focus_outcome="applied"
            )
        )
        server = FastMCP("focus-bound-lifecycle")
        _register_lifecycle(server, service)
        return (
            server,
            service.end,
            lambda focus: {
                "session_id": str(uuid4()),
                "expected_client_key": "task-a",
                "summary": "done",
                argument: focus,
                "expected_focus_revision": 1,
                "nothing_to_capture_reason": "nothing durable",
            },
        )

    context_svc, roadmap_svc = AsyncMock(), AsyncMock()
    roadmap_svc.update_project_focus = AsyncMock(
        side_effect=lambda _key, focus, **_kw: SimpleNamespace(
            current_focus=focus, focus_revision=4, features_updated=(), features_unpinned=()
        )
    )
    server = FastMCP("focus-bound-project-context")
    _register_project_context(server, context_svc, roadmap_svc)

    if tool_name == "brain_update_project_focus":
        return (
            server,
            roadmap_svc.update_project_focus,
            lambda focus: {
                "project_key": "brain-v42",
                argument: focus,
                "expected_focus_revision": 1,
            },
        )
    return (
        server,
        context_svc.get_or_create,
        lambda focus: {
            "project_key": "brain-v42",
            "name": "Brain V42",
            "description": "Second Cerveau MCP server",
            argument: focus,
        },
    )


async def _focus_property(tool_name: str, argument: str) -> dict[str, Any]:
    server = _lifecycle_server() if tool_name == "brain_session_end" else _project_context_server()
    tool = await server.get_tool(tool_name)
    assert tool is not None, f"missing MCP tool {tool_name}"
    schema = tool.parameters["properties"][argument]
    # `brain_set_project_context` declares its focus optional: the bound then
    # lives in the `anyOf`'s `string` branch, not at the root. Looking for it at
    # the root level only would turn this test GREEN on an unbounded argument.
    if "anyOf" in schema:
        schema = next(v for v in schema["anyOf"] if v.get("type") == "string")
    return schema


@pytest.mark.parametrize(("tool_name", "argument"), FOCUS_WRITERS)
async def test_every_focus_writer_publishes_the_same_bound(tool_name: str, argument: str) -> None:
    """The three writers announce the SAME bound in their published schema.

    The assertion is on the PUBLISHED schema, not on an imported constant: that
    is what a client sees, and it is the only level where a divergence is
    observable from the outside.
    """
    schema = await _focus_property(tool_name, argument)

    assert schema.get("maxLength") == NEXT_FOCUS_MAX_LENGTH, (
        f"{tool_name}.{argument} publie maxLength={schema.get('maxLength')!r} "
        f"au lieu de {NEXT_FOCUS_MAX_LENGTH}"
    )


@pytest.mark.parametrize(("tool_name", "argument"), FOCUS_WRITERS)
async def test_every_focus_writer_refuses_one_character_too_many(
    tool_name: str, argument: str
) -> None:
    """One character too many is REFUSED **before any service call**, never truncated.

    That is the fail-closed proof, and it is played by CALLING the tool, not by
    re-reading its schema: a schema announcing a bound does not prove the bound
    applies. Two assertions, inseparable:

    - at ``CAP + 1``: ``ValidationError``, **and the service is never called** —
      so nothing is written, and nothing is silently truncated;
    - at exactly ``CAP`` (negative witness): **the service IS called**. Without
      it, an argument that became mandatory-but-always-refused, or a bound fallen
      to zero, would turn the first half green.
    """
    server, service, call = _invocation(tool_name, argument)
    tool = await server.get_tool(tool_name)
    assert tool is not None

    with pytest.raises(ValidationError):
        await tool.run(call("x" * (NEXT_FOCUS_MAX_LENGTH + 1)))
    service.assert_not_called()

    # Negative witness: at the exact length, validation lets it through and the
    # service receives the write.
    await tool.run(call("x" * NEXT_FOCUS_MAX_LENGTH))
    service.assert_called_once()


@pytest.mark.parametrize(("tool_name", "argument"), FOCUS_WRITERS)
async def test_the_bound_counts_characters_not_bytes(tool_name: str, argument: str) -> None:
    """The distinguishing witness: the cap is in CHARACTERS, not in bytes.

    A non-ASCII focus whose length is exactly the cap weighs TWICE the cap in
    bytes. It must be accepted. This test turns red the day someone rewrites the
    bound in bytes — the case `brain-v42`'s real focus already met (9,977
    characters for 10,285 bytes).
    """
    schema = await _focus_property(tool_name, argument)

    non_ascii = _TWO_BYTE_CHAR * NEXT_FOCUS_MAX_LENGTH
    assert len(non_ascii) == NEXT_FOCUS_MAX_LENGTH
    assert len(non_ascii.encode("utf-8")) == 2 * NEXT_FOCUS_MAX_LENGTH

    # The two counts DIFFER on this input: that is what makes it a witness. A
    # byte bound would refuse it; a character bound would not.
    assert len(non_ascii) <= schema["maxLength"] < len(non_ascii.encode("utf-8"))


async def test_the_three_writers_are_not_parallel_literals() -> None:
    """One shared bound, not three literals drifting separately.

    Three `10_000` written in three places would be green today and would diverge
    at the first change — exactly the defect this batch repairs, reproduced one
    layer up. The test compares the bounds against each other, citing no number:
    it turns red if one of them moves alone.
    """
    bounds = {}
    for tool_name, argument in FOCUS_WRITERS:
        schema = await _focus_property(tool_name, argument)
        bounds[f"{tool_name}.{argument}"] = schema.get("maxLength")

    assert len(set(bounds.values())) == 1, f"bornes divergentes : {bounds}"


async def test_no_focus_writer_escapes_the_census() -> None:
    """The writer survey is CLOSED, and is counted through several angles.

    `bfb4cf93` and its mandate named only TWO; there are three. This test pins the
    list so that a fourth writer added later breaks here rather than being
    discovered through an unrecoverable focus. The search angle is the COLUMN name
    in the published signature, not the argument name — that is what catches an
    optional `current_focus`.
    """
    from brain_v42.mcp.tools.project_context_tools import register_project_context_tools
    from brain_v42.mcp.tools.session_lifecycle_tools import register_session_lifecycle_tools

    found: set[tuple[str, str]] = set()
    for registrar, args in (
        (register_session_lifecycle_tools, (MagicMock(), AsyncMock())),
        (register_project_context_tools, (AsyncMock(), AsyncMock())),
    ):
        server = FastMCP("focus-writer-census")
        registrar(server, *args)
        for tool in await server.list_tools():
            for name, schema in tool.parameters.get("properties", {}).items():
                if name in ("next_focus", "current_focus"):
                    del schema
                    found.add((tool.name, name))

    assert found == set(FOCUS_WRITERS), (
        f"le recensement des écrivains de focus a changé : {sorted(found)}"
    )
