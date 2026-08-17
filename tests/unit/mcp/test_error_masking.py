"""Tests for mask_error_details — DSN/path/traceback leakage prevention.

Behavioural contract (transport-agnostic, in-memory client):
1. Unexpected RuntimeError raised inside a tool -> client receives a generic message
   (no DSN, no traceback frame, no class name) when mask_error_details=True.
2. Explicitly-raised ToolError("safe message") -> client still receives "safe message"
   (masking only hides *unexpected* internals, not deliberate ToolError messages).
3. The production brain FastMCP instance has mask_error_details=True.
"""

from __future__ import annotations

import pytest
import pytest_asyncio
from fastmcp import Client, FastMCP
from fastmcp.exceptions import ToolError

_DSN_FRAGMENT = "@localhost:5433"
_DSN = f"connection to postgresql+asyncpg://brain:brain{_DSN_FRAGMENT}/brain failed"


@pytest_asyncio.fixture()
async def masked_app() -> FastMCP:
    """FastMCP instance with mask_error_details=True (mirrors the brain server)."""
    app = FastMCP("test-masked", mask_error_details=True)

    @app.tool()
    async def leaky_tool() -> str:
        raise RuntimeError(_DSN)

    @app.tool()
    async def safe_tool_error() -> str:
        raise ToolError("safe message")

    return app


@pytest_asyncio.fixture()
async def unmasked_app() -> FastMCP:
    """FastMCP instance with mask_error_details=False -- proves the knob matters."""
    app = FastMCP("test-unmasked", mask_error_details=False)

    @app.tool()
    async def leaky_tool() -> str:
        raise RuntimeError(_DSN)

    return app


@pytest.mark.anyio
async def test_unmasked_leaks_dsn(unmasked_app: FastMCP) -> None:
    """Without masking, the DSN fragment IS visible in the surfaced error -- proving the knob matters."""
    async with Client(unmasked_app) as client:
        with pytest.raises(ToolError) as exc_info:
            await client.call_tool("leaky_tool", {})
        assert _DSN_FRAGMENT in str(exc_info.value), (
            f"Expected DSN fragment '{_DSN_FRAGMENT}' to leak when mask_error_details=False"
        )


@pytest.mark.anyio
async def test_masked_hides_dsn(masked_app: FastMCP) -> None:
    """With masking, DSN fragment must NOT appear in the surfaced error."""
    async with Client(masked_app) as client:
        with pytest.raises(ToolError) as exc_info:
            await client.call_tool("leaky_tool", {})
        error_text = str(exc_info.value)
        assert _DSN_FRAGMENT not in error_text, (
            f"DSN fragment '{_DSN_FRAGMENT}' leaked to client despite mask_error_details=True"
        )


@pytest.mark.anyio
async def test_masked_hides_runtime_error_class_name(masked_app: FastMCP) -> None:
    """The class name 'RuntimeError' must not appear in the masked error message."""
    async with Client(masked_app) as client:
        with pytest.raises(ToolError) as exc_info:
            await client.call_tool("leaky_tool", {})
        assert "RuntimeError" not in str(exc_info.value)


@pytest.mark.anyio
async def test_explicit_tool_error_still_surfaces(masked_app: FastMCP) -> None:
    """Explicitly-raised ToolError messages MUST still reach the client even with masking."""
    async with Client(masked_app) as client:
        with pytest.raises(ToolError) as exc_info:
            await client.call_tool("safe_tool_error", {})
        assert "safe message" in str(exc_info.value), (
            "ToolError message was incorrectly suppressed by mask_error_details=True"
        )


def test_brain_server_has_mask_error_details_enabled() -> None:
    """The production brain FastMCP instance is constructed with mask_error_details=True."""
    from brain_v42.mcp import server

    assert getattr(server.mcp, "_mask_error_details", None) is True, (
        "brain FastMCP instance must have mask_error_details=True to prevent DSN/path leakage"
    )
