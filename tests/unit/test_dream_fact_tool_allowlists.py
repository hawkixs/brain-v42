"""Facts stay available to operators, not to autonomous Dream phases."""

from __future__ import annotations

import pytest

from brain_v42.mcp.dream_capabilities import (
    DREAM_PHASE_TOOL_ALLOWLISTS,
    dream_phase_tool_allowlist,
)


@pytest.mark.parametrize("phase", sorted(DREAM_PHASE_TOOL_ALLOWLISTS))
def test_no_dream_phase_allowlist_grants_measured_fact_tools(phase: str) -> None:
    """Dream phases cannot trigger reads that may consume a fact refresh budget."""
    allowed = dream_phase_tool_allowlist(phase)

    assert "brain_fact_get" not in allowed
    assert "brain_fact_list" not in allowed
