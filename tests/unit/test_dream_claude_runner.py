"""Contract tests for the isolated Claude Dream phase runner.

The Claude rail existed as an operator rollback, but it could not carry a
per-``(project, phase)`` capability bearer: it reused the repository's static
``.mcp.json``, whose ``Authorization`` expands ``${MCP_HTTP_TOKEN}`` — the
ADMIN token.  ``dream.sh`` therefore refused Claude outright whenever
``BRAIN_DREAM_CAPABILITY_ENFORCEMENT`` was on, because running it would have
handed six unscoped phases the full tool surface while the logs still read
green.

This runner closes that gap so the refusal can be lifted rather than merely
deleted.  It owns the same process boundary as ``codex_runner``: scoped
bearer, exact per-phase tool allowlist, loopback-pinned MCP URL, and a hard
process-group timeout.
"""

from __future__ import annotations

import importlib
import inspect
import json
from pathlib import Path
from types import ModuleType

import pytest

REPO_ROOT = Path(__file__).resolve().parents[2]
RUNNER_PATH = REPO_ROOT / "scripts" / "dream" / "claude_runner.py"

PHASES = ("scan", "clean", "connect", "synth", "promote", "reorg")

# Exported by dream.sh for the Claude rail alone. The first three feed
# otel_split (a Claude phase with no telemetry yields an empty otel log and a
# metrics row that silently loses its token counts). The last two are the fix
# for regression 27430ae1: without them `claude -p` snapshots its tool list
# ~450ms after init, before the brain-v42 MCP server has registered, and the
# phase runs BLIND while still exiting 0.
CLAUDE_RAIL_ENVIRONMENT = (
    "CLAUDE_CODE_ENABLE_TELEMETRY",
    "OTEL_LOGS_EXPORTER",
    "OTEL_METRICS_EXPORTER",
    "MCP_CONNECTION_NONBLOCKING",
    "MCP_CONNECT_TIMEOUT_MS",
)

ADMIN_TOKEN = "admin-token-never-scoped"


def _runner() -> ModuleType:
    assert RUNNER_PATH.is_file(), (
        "Claude Dream runner is missing: expected scripts/dream/claude_runner.py"
    )
    return importlib.import_module("scripts.dream.claude_runner")


def _capability_registry(*, project_key: str = "brain-v42") -> str:
    profiles = {
        f"{project_key}:{phase}": {
            "active": f"{phase}-active-token",
            "accepted": [f"{phase}-accepted-token"] if phase == "scan" else [],
        }
        for phase in PHASES
    }
    return json.dumps(profiles)


def _enforced_environment(**overrides: str) -> dict[str, str]:
    environment = {
        "BRAIN_DREAM_CAPABILITY_ENFORCEMENT": "true",
        "MCP_HTTP_TOKEN": ADMIN_TOKEN,
        "MCP_HTTP_DREAM_TOKENS": _capability_registry(),
        "HOME": "/home/hawixs",
        "PATH": "/usr/bin:/bin",
        "CLAUDE_CODE_ENABLE_TELEMETRY": "1",
        "OTEL_LOGS_EXPORTER": "console",
        "OTEL_METRICS_EXPORTER": "console",
        "MCP_CONNECTION_NONBLOCKING": "false",
        "MCP_CONNECT_TIMEOUT_MS": "10000",
    }
    environment.update(overrides)
    return environment


def test_child_environment_carries_the_phase_scoped_bearer_not_the_admin_token() -> None:
    """The whole point of the runner: six phases, six different bearers."""
    runner = _runner()

    for phase in PHASES:
        child = runner.claude_child_environment(
            project_key="brain-v42",
            phase=phase,
            environ=_enforced_environment(),
        )

        assert child is not None
        assert child["MCP_HTTP_TOKEN"] == f"{phase}-active-token"
        assert child["MCP_HTTP_TOKEN"] != ADMIN_TOKEN


def test_claude_rail_environment_survives_into_the_child() -> None:
    """Regression 27430ae1 and otel_split both live in these five variables.

    Reusing the Codex child allowlist verbatim would drop them: the phase would
    still exit 0, with no telemetry and an intermittently empty brain tool list.
    """
    runner = _runner()

    child = runner.claude_child_environment(
        project_key="brain-v42",
        phase="scan",
        environ=_enforced_environment(),
    )

    assert child is not None
    for variable in CLAUDE_RAIL_ENVIRONMENT:
        assert variable in child, f"{variable} must reach the claude child process"
    assert child["MCP_CONNECTION_NONBLOCKING"] == "false"


def test_admin_token_never_reaches_the_child_under_enforcement() -> None:
    runner = _runner()

    child = runner.claude_child_environment(
        project_key="brain-v42",
        phase="reorg",
        environ=_enforced_environment(),
    )

    assert child is not None
    assert ADMIN_TOKEN not in child.values()
    assert "MCP_HTTP_DREAM_TOKENS" not in child, "the whole registry must not leak to the child"


def test_mcp_config_references_the_token_by_environment_and_never_writes_it() -> None:
    """No secret on disk and none in argv — the same trade Codex makes."""
    runner = _runner()

    config = runner.build_claude_mcp_config(phase="scan", mcp_url="http://127.0.0.1:8765/mcp")
    serialized = json.dumps(config)

    assert "${MCP_HTTP_TOKEN}" in serialized
    assert "scan-active-token" not in serialized
    assert ADMIN_TOKEN not in serialized


def test_mcp_config_declares_only_brain_v42_on_loopback() -> None:
    runner = _runner()

    config = runner.build_claude_mcp_config(phase="synth", mcp_url="http://127.0.0.1:8765/mcp")

    assert set(config["mcpServers"]) == {"brain-v42"}
    assert config["mcpServers"]["brain-v42"]["url"] == "http://127.0.0.1:8765/mcp"


def test_mcp_config_labels_the_agent_per_phase() -> None:
    """The sidecar attributes activity by X-Brain-Agent; a shared label erases
    which phase produced which observation."""
    runner = _runner()

    for phase in PHASES:
        config = runner.build_claude_mcp_config(phase=phase, mcp_url="http://127.0.0.1:8765/mcp")
        headers = config["mcpServers"]["brain-v42"]["headers"]

        assert headers["X-Brain-Agent"] == f"dream-claude-{phase}"


def test_command_restricts_tools_to_the_phase_allowlist() -> None:
    runner = _runner()
    from brain_v42.mcp.dream_capabilities import dream_phase_tool_allowlist

    for phase in PHASES:
        command = runner.build_claude_command(
            phase=phase,
            model="sonnet",
            max_turns=30,
            mcp_config_path=Path("/tmp/mcp.json"),
        )

        allowed = command[command.index("--allowedTools") + 1]
        expected = {f"mcp__brain-v42__{tool}" for tool in dream_phase_tool_allowlist(phase)}

        assert set(allowed.split(",")) == expected
        assert "mcp__brain-v42__*" not in allowed, (
            "the wildcard is what the capability firewall exists to remove"
        )


def test_command_keeps_the_strict_mcp_and_no_builtin_tool_boundary() -> None:
    runner = _runner()

    command = runner.build_claude_command(
        phase="scan",
        model="sonnet",
        max_turns=30,
        mcp_config_path=Path("/tmp/mcp.json"),
    )

    assert "--strict-mcp-config" in command
    assert command[command.index("--tools") + 1] == ""
    assert command[command.index("--mcp-config") + 1] == "/tmp/mcp.json"


def test_unknown_phase_fails_closed() -> None:
    runner = _runner()

    with pytest.raises(ValueError, match="unsupported Dream phase"):
        runner.build_claude_mcp_config(phase="nonexistent", mcp_url="http://127.0.0.1:8765/mcp")


def test_missing_profile_for_the_project_fails_closed_instead_of_falling_back() -> None:
    """A project absent from the registry must not silently borrow the admin
    token — that is precisely the hole dream.sh's refusal was plugging."""
    runner = _runner()
    from brain_v42.mcp.dream_capabilities import DreamCapabilityConfigurationError

    with pytest.raises(DreamCapabilityConfigurationError):
        runner.claude_child_environment(
            project_key="a-project-with-no-profile",
            phase="scan",
            environ=_enforced_environment(),
        )


def test_non_loopback_mcp_url_fails_closed() -> None:
    runner = _runner()
    from brain_v42.mcp.dream_capabilities import DreamCapabilityConfigurationError

    with pytest.raises(DreamCapabilityConfigurationError):
        runner.claude_child_environment(
            project_key="brain-v42",
            phase="scan",
            environ=_enforced_environment(BRAIN_DREAM_MCP_URL="http://192.168.1.12:8765/mcp"),
        )


def test_enforcement_disabled_leaves_the_ambient_environment_untouched() -> None:
    """With the killswitch closed the rail must behave exactly as before, so
    the rollback path stays available on a host with no registry at all."""
    runner = _runner()

    child = runner.claude_child_environment(
        project_key="brain-v42",
        phase="scan",
        environ={"BRAIN_DREAM_CAPABILITY_ENFORCEMENT": "false", "MCP_HTTP_TOKEN": ADMIN_TOKEN},
    )

    assert child is None


def test_runner_exposes_a_project_scoped_api() -> None:
    runner = _runner()

    parameters = inspect.signature(runner.run_claude).parameters
    assert "project_key" in parameters
    assert "phase" in parameters
