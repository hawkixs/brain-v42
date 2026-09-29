"""The local verify step must not acquire a Dream MCP capability."""

from brain_v42.mcp.dream_capabilities import DREAM_PHASE_TOOL_ALLOWLISTS
from brain_v42.services.dream_project_scope import _DREAM_PHASES


def test_verify_has_no_dream_capability_or_project_scope() -> None:
    assert "verify" not in DREAM_PHASE_TOOL_ALLOWLISTS
    assert all("brain_claim_verify" not in tools for tools in DREAM_PHASE_TOOL_ALLOWLISTS.values())
    assert _DREAM_PHASES == frozenset(("scan", "clean", "connect", "synth", "promote", "reorg"))
