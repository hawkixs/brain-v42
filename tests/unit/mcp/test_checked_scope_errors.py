"""Repository scope races retain the public refusal through production wrappers."""

import pytest
from fastmcp import Client, FastMCP

from brain_v42.credentials.author_binding import author_binding_counts, reset_author_binding_counts
from brain_v42.credentials.reasons import refusal_counts, reset_refusal_counts
from brain_v42.mcp.business_errors import surface_business_errors
from brain_v42.mcp.tool_catalog import apply_tool_catalog_profile
from brain_v42.repositories.checked_project import CheckedProjectMismatch


@pytest.mark.parametrize("compact", [False, True])
async def test_checked_mismatch_is_one_uniform_public_refusal(compact):
    reset_author_binding_counts()
    reset_refusal_counts()
    mcp = FastMCP(mask_error_details=True)

    @mcp.tool()
    async def brain_update() -> str:
        raise CheckedProjectMismatch

    await surface_business_errors(mcp)
    if compact:
        apply_tool_catalog_profile(mcp, profile="compact")
    async with Client(mcp) as client:
        result = await client.call_tool(
            "brain_call_tool" if compact else "brain_update",
            {"name": "brain_update", "arguments": {}} if compact else {},
            raise_on_error=False,
        )
    assert "scope_denied" in str(result.content)
    assert author_binding_counts() == {"brain_update:scope_mismatch": 1}
    assert refusal_counts() == {"scope_denied": 1}
