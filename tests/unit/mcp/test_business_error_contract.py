"""Transport contract for expected MCP business failures."""

from __future__ import annotations

import pytest
from fastmcp import Client, FastMCP
from fastmcp.exceptions import ToolError

from brain_v42.maintenance.plan_index_repair import RepairSafetyError
from brain_v42.mcp.business_errors import surface_business_errors
from brain_v42.mcp.tool_catalog import apply_tool_catalog_profile
from brain_v42.mcp.tools.formatters import format_error
from brain_v42.models.brain_session import BrainSessionStateError
from brain_v42.services.consolidation import ConsolidationEntityNotFoundError
from brain_v42.services.entity_maintenance_service import UnknownEntityTypeError
from brain_v42.services.feature_creation_service import FeatureAlreadyExistsError
from brain_v42.services.feature_service import FeatureStateConflictError
from brain_v42.services.plan_indexer import PlanScanPathError
from brain_v42.services.proposal_service import ProposalNotFoundError
from brain_v42.services.roadmap_service import ProjectFocusConflictError
from brain_v42.services.ticket_service import UnknownProjectError

_UNKNOWN_PROJECT_MESSAGE = (
    "Unknown project 'projet-qui-nexiste-pas' — create it first "
    "(brain_set_project_context) or check the key (brain_list_projects)"
)
_DSN_FRAGMENT = "@localhost:5433"


@pytest.fixture()
def business_error_app() -> FastMCP:
    """Expose one real ``format_error`` call through a masked FastMCP server."""
    app = FastMCP("business-error-contract", mask_error_details=True)

    @app.tool()
    async def missing_entity() -> str:
        return format_error("learning not found; valid types: decision, learning")

    return app


async def test_business_failure_sets_mcp_error_and_preserves_safe_details(
    business_error_app: FastMCP,
) -> None:
    """Returning a success string must not hide an expected business failure."""
    async with Client(business_error_app) as client:
        result = await client.call_tool("missing_entity", {}, raise_on_error=False)

        assert result.is_error is True
        assert "learning not found; valid types: decision, learning" in str(result.content)

        with pytest.raises(ToolError, match="learning not found; valid types"):
            await client.call_tool("missing_entity", {})


# ---------------------------------------------------------------------------
# Uncaught business exceptions (ticket 40ab2ced)
#
# ``format_error`` only covers failures a tool explicitly catches. Guards like
# ``require_known_project`` raise from deep inside a service and escape the
# tool body uncaught, so ``mask_error_details=True`` flattens their actionable
# text to "Error calling tool 'X'". These tests pin the opposite contract.
# ---------------------------------------------------------------------------


@pytest.fixture()
async def uncaught_error_app() -> FastMCP:
    """Mirror production: a masked server whose tools let exceptions escape."""
    app = FastMCP("uncaught-business-error", mask_error_details=True)

    @app.tool()
    async def unknown_project() -> str:
        raise UnknownProjectError(_UNKNOWN_PROJECT_MESSAGE)

    @app.tool()
    async def internal_failure() -> str:
        raise RuntimeError(f"connection to postgresql+asyncpg://brain:brain{_DSN_FRAGMENT}/brain")

    await surface_business_errors(app)
    return app


async def test_uncaught_business_exception_surfaces_actionable_message(
    uncaught_error_app: FastMCP,
) -> None:
    """A guard rejection must name the offending key and the corrective tools."""
    async with Client(uncaught_error_app) as client:
        with pytest.raises(ToolError) as exc_info:
            await client.call_tool("unknown_project", {})

    text = str(exc_info.value)
    assert "projet-qui-nexiste-pas" in text, (
        f"the refused project key was flattened away by the MCP layer: {text!r}"
    )
    assert "brain_set_project_context" in text, (
        f"the corrective action was flattened away by the MCP layer: {text!r}"
    )


async def test_uncaught_internal_exception_stays_masked(
    uncaught_error_app: FastMCP,
) -> None:
    """Surfacing business errors must not degrade into a blanket passthrough."""
    async with Client(uncaught_error_app) as client:
        with pytest.raises(ToolError) as exc_info:
            await client.call_tool("internal_failure", {})

    text = str(exc_info.value)
    assert _DSN_FRAGMENT not in text, f"DSN leaked to the client: {text!r}"
    assert "RuntimeError" not in text, f"internal class name leaked to the client: {text!r}"


@pytest.mark.parametrize(
    "exc",
    [
        pytest.param(UnknownProjectError("marker-ticket"), id="ticket"),
        pytest.param(BrainSessionStateError("marker-session"), id="session-lifecycle"),
        pytest.param(FeatureAlreadyExistsError("marker-feature"), id="feature-creation"),
        pytest.param(FeatureStateConflictError("marker-feature-state"), id="feature-state"),
        pytest.param(
            ProposalNotFoundError("marker-proposal", family="roadmap_curation", proposal_id=7),
            id="proposal",
        ),
        pytest.param(
            ProjectFocusConflictError(current_focus="marker-focus", current_revision=192),
            id="project-focus",
        ),
        pytest.param(UnknownEntityTypeError("marker-entity"), id="entity-maintenance"),
        pytest.param(ConsolidationEntityNotFoundError("marker-consolidation"), id="consolidation"),
    ],
)
async def test_whitelisted_business_families_surface_their_message(exc: Exception) -> None:
    """Every whitelisted family reaches the caller with its own text intact."""
    app = FastMCP("whitelist-family", mask_error_details=True)

    @app.tool()
    async def failing() -> str:
        raise exc

    await surface_business_errors(app)

    async with Client(app) as client:
        with pytest.raises(ToolError) as exc_info:
            await client.call_tool("failing", {})

    assert str(exc) in str(exc_info.value)


@pytest.mark.parametrize(
    "exc",
    [
        pytest.param(
            PlanScanPathError("/home/hawixs/secret/plans", "symlink_refused"),
            id="filesystem-path",
        ),
        pytest.param(RepairSafetyError("marker-ops-internal"), id="ops-internal"),
    ],
)
async def test_non_whitelisted_families_stay_masked(exc: Exception) -> None:
    """Filesystem and ops/admin failures are deliberately excluded from the whitelist."""
    app = FastMCP("whitelist-exclusion", mask_error_details=True)

    @app.tool()
    async def failing() -> str:
        raise exc

    await surface_business_errors(app)

    async with Client(app) as client:
        with pytest.raises(ToolError) as exc_info:
            await client.call_tool("failing", {})

    assert str(exc) not in str(exc_info.value), (
        f"{type(exc).__name__} must not reach the caller: {exc_info.value!r}"
    )


async def test_wrapping_preserves_tool_input_schema() -> None:
    """The wrapper must stay signature-transparent: FastMCP derives the schema from ``fn``."""
    app = FastMCP("schema-preservation", mask_error_details=True)

    @app.tool()
    async def documented(count: int, label: str = "default") -> str:
        return f"{count}{label}"

    before = [t.parameters for t in await app._list_tools()]
    await surface_business_errors(app)
    after = [t.parameters for t in await app._list_tools()]

    assert before == after, f"wrapping changed the published input schema: {before} -> {after}"

    async with Client(app) as client:
        result = await client.call_tool("documented", {"count": 5, "label": "z"})
    assert "5z" in str(result.content)


async def test_surfacing_is_idempotent() -> None:
    """A second pass must not stack wrappers — registration order is not guaranteed."""
    app = FastMCP("idempotence", mask_error_details=True)

    @app.tool()
    async def unknown_project() -> str:
        raise UnknownProjectError(_UNKNOWN_PROJECT_MESSAGE)

    first = await surface_business_errors(app)
    second = await surface_business_errors(app)

    assert first == ("unknown_project",)
    assert second == (), f"second pass re-wrapped already-surfaced tools: {second}"


async def test_compact_gateway_surfaces_business_message_exactly_once() -> None:
    """The compact gateway re-enters the tool chain; the text must not double up."""
    app = FastMCP("gateway-business-error", mask_error_details=True)

    @app.tool()
    async def unknown_project() -> str:
        raise UnknownProjectError(_UNKNOWN_PROJECT_MESSAGE)

    await surface_business_errors(app)
    apply_tool_catalog_profile(app, "compact")

    async with Client(app) as client:
        with pytest.raises(ToolError) as exc_info:
            await client.call_tool(
                "brain_call_tool",
                {"name": "unknown_project", "arguments": {}},
            )

    text = str(exc_info.value)
    assert "projet-qui-nexiste-pas" in text, (
        f"the compact gateway flattened the business message: {text!r}"
    )
    assert text.count("brain_set_project_context") == 1, (
        f"the message was wrapped twice by gateway re-entrance: {text!r}"
    )
