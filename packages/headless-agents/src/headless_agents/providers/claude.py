"""Isolated ``claude -p`` adapter.

Ported from ``brain_v42.agents.providers.claude`` with the policy inverted:
the MCP server, its bearer variable, its headers and its tool allowlist arrive
as a :class:`~headless_agents.profile.McpServer` (or ``None`` for a run that
may reach no server), and the child environment is whatever the caller built.

The adapter renders a per-run MCP client configuration (no wildcard) into a
private temporary directory and hands the child process the bearer under the
name the configuration references, through ``environment``. The secret is
therefore never written to disk and never appears in ``argv`` -- the same
trade the Codex adapter makes.
"""

from __future__ import annotations

import json
import os
import subprocess
import tempfile
import time
from collections.abc import Mapping
from pathlib import Path

from ..capability import (
    PROVIDER_FALLBACK_EXIT_CODE,
    TIMEOUT_EXIT_CODE,
    failure_code_after_a_write,
    terminate_process_group,
)
from ..profile import McpServer
from ..result import RunResult
from ..spec import RunSpec

# Ambient variables this rail needs on top of the base allowlist.
#
# The three OTEL variables are what a telemetry splitter consumes; without
# them a run still exits 0 but loses every token count -- and, worse, loses
# the tool_result records :func:`tool_call_completed` reads.
#
# MCP_CONNECTION_NONBLOCKING and MCP_CONNECT_TIMEOUT_MS: ``claude -p``
# otherwise snapshots the turn's tool list ~450 ms after init, before a slow
# MCP server has registered its tools. The run then proceeds with NO tools and
# reports success -- the false-green failure this rail has paid for twice.
CHILD_ENV_PASSTHROUGH = frozenset(
    {
        "CLAUDE_CODE_ENABLE_TELEMETRY",
        "OTEL_LOGS_EXPORTER",
        "OTEL_METRICS_EXPORTER",
        "OTEL_EXPORTER_OTLP_ENDPOINT",
        "OTEL_EXPORTER_OTLP_PROTOCOL",
        "MCP_CONNECTION_NONBLOCKING",
        "MCP_CONNECT_TIMEOUT_MS",
        "CLAUDE_CONFIG_DIR",
        "ANTHROPIC_API_KEY",
        "ANTHROPIC_AUTH_TOKEN",
    }
)


def build_claude_mcp_config(mcp: McpServer | None) -> dict[str, object]:
    """Render the per-run MCP client configuration for ``claude -p``.

    ``Authorization`` is written as ``${VAR}``, expanded by the client from the
    child environment: the bearer value itself never reaches the file.
    """
    if mcp is None:
        return {"mcpServers": {}}
    return {
        "mcpServers": {
            mcp.name: {
                "type": "http",
                "url": mcp.url,
                "headers": {
                    **dict(mcp.headers),
                    "Authorization": f"Bearer ${{{mcp.bearer_env_var}}}",
                },
            }
        }
    }


def build_claude_command(
    *,
    model: str,
    max_turns: int,
    mcp_config_path: Path,
    mcp: McpServer | None,
    executable: str = "claude",
) -> list[str]:
    """Build the hardened non-interactive Claude command for one run.

    The exact per-run allowlist replaces any ``mcp__<server>__*`` wildcard: the
    wildcard is what a scoped bearer exists to make unnecessary, and leaving it
    would make the bearer the only line of defence. No server, no
    ``--allowedTools`` at all.
    """
    if not model.strip():
        raise ValueError("Claude model must not be empty")
    if max_turns <= 0:
        raise ValueError("max_turns must be positive")

    command = [
        executable,
        "-p",
        "-",
        "--model",
        model,
        "--max-turns",
        str(max_turns),
        "--permission-mode",
        "bypassPermissions",
        "--tools",
        "",
    ]
    if mcp is not None:
        allowed_tools = ",".join(f"mcp__{mcp.name}__{tool}" for tool in mcp.tools)
        command.extend(("--allowedTools", allowed_tools))
    command.extend(("--mcp-config", str(mcp_config_path), "--strict-mcp-config"))
    return command


def tool_call_completed(raw_log: Path) -> bool:
    """Did an MCP tool call SUCCEED in this OTEL telemetry?

    On the only source the claude rail exposes: the OTEL console stream mixed
    into ``raw_log``. A shape MEASURED against claude 2.1.226, not from a doc:

        body: "claude_code.tool_result"
        attributes: { tool_name: "mcp_tool", success: "true", ... }

    ``tool_name`` is generically ``mcp_tool`` -- it does NOT name the server.
    That is no gap as long as the run declares ONE server under
    ``--strict-mcp-config``: any successful MCP result is then necessarily a
    call on it. A caller declaring several servers on this rail must tighten
    this predicate first.
    """
    if not raw_log.is_file():
        return False
    content = raw_log.read_text(encoding="utf-8", errors="replace")
    # The console stream is multi-line pseudo-JSON, not JSON: we split on the
    # record rather than parsing it.
    for record in content.split('body: "claude_code.tool_result"')[1:]:
        window = record[:2000]
        if 'tool_name: "mcp_tool"' in window and 'success: "true"' in window:
            return True
    return False


def _effective_timeout(timeout_seconds: float, deadline: float | None) -> float:
    if deadline is None:
        return timeout_seconds
    return max(0.0, min(timeout_seconds, deadline - time.monotonic()))


def run_claude(
    *,
    prompt: str,
    model: str,
    max_turns: int,
    timeout_seconds: float,
    raw_log: Path,
    mcp: McpServer | None,
    environment: Mapping[str, str] | None = None,
    executable: str = "claude",
    deadline: float | None = None,
    temp_prefix: str = "headless-agents-claude-",
) -> int:
    """Run one Claude invocation and return its exit code (``124`` on timeout).

    stdout and stderr land MIXED in ``raw_log``: the OTEL console stream a
    telemetry splitter consumes travels on stderr alongside the answer, and
    separating them here would silently deprive that consumer of it.
    """
    if timeout_seconds <= 0:
        raise ValueError("timeout_seconds must be positive")
    # ``None`` inherits: Popen is then called with ``env=None``.
    child_environment = dict(environment) if environment is not None else None
    visible = child_environment if child_environment is not None else os.environ
    if mcp is not None and not visible.get(mcp.bearer_env_var):
        raw_log.parent.mkdir(parents=True, exist_ok=True)
        with raw_log.open("a", encoding="utf-8") as stream:
            stream.write(f"missing required environment variable: {mcp.bearer_env_var}\n")
        return 1

    raw_log = raw_log.resolve()
    raw_log.parent.mkdir(parents=True, exist_ok=True)

    with tempfile.TemporaryDirectory(prefix=temp_prefix) as temp_dir:
        runtime_dir = Path(temp_dir)
        mcp_config_path = runtime_dir / "mcp-config.json"
        mcp_config_path.write_text(json.dumps(build_claude_mcp_config(mcp)), encoding="utf-8")
        command = build_claude_command(
            model=model,
            max_turns=max_turns,
            mcp_config_path=mcp_config_path,
            mcp=mcp,
            executable=executable,
        )

        with raw_log.open("a", encoding="utf-8") as raw_stream:
            try:
                process = subprocess.Popen(
                    command,
                    stdin=subprocess.PIPE,
                    stdout=raw_stream,
                    stderr=subprocess.STDOUT,
                    cwd=runtime_dir,
                    env=child_environment,
                    text=True,
                    start_new_session=True,
                )
            except OSError as exc:
                raw_stream.write(f"unable to start Claude: {exc}\n")
                # Claude did not start: nothing could have been written.
                return PROVIDER_FALLBACK_EXIT_CODE

            try:
                process.communicate(
                    input=prompt, timeout=_effective_timeout(timeout_seconds, deadline)
                )
            except subprocess.TimeoutExpired:
                terminate_process_group(process)
                # A timeout proves NOTHING here, and this rail cannot make it
                # prove more: its only witness is the OTEL console stream,
                # which a batch exporter flushes on an interval, so an empty
                # raw_log at the kill does not show an empty run -- the first
                # tool call may sit unflushed. The three JSON rails write their
                # events as they happen and can return
                # TIMEOUT_REPLAYABLE_EXIT_CODE on a stream that never started;
                # claude keeps the plain 124. Never a switchover here.
                return TIMEOUT_EXIT_CODE

    exit_code = int(process.returncode or 0)
    if exit_code == 0:
        return 0
    if exit_code == TIMEOUT_EXIT_CODE:
        return TIMEOUT_EXIT_CODE
    # The claude rail cannot prove the absence of a write other than through
    # its telemetry: without the OTEL_* variables in the child environment,
    # raw_log holds no tool event at all and the predicate returns False. That
    # is the right default -- it allows the switchover on a rail that plainly
    # did nothing -- and it is also why those variables sit in
    # CHILD_ENV_PASSTHROUGH.
    if tool_call_completed(raw_log):
        return failure_code_after_a_write(exit_code)
    return PROVIDER_FALLBACK_EXIT_CODE


class ClaudeProvider:
    """:class:`~headless_agents.protocol.AgentProvider` adapter over Claude."""

    name = "claude"

    def build_command(self, spec: RunSpec) -> list[str]:
        mcp_config_path = spec.extra.get("mcp_config_path")
        assert isinstance(mcp_config_path, Path), (
            "RunSpec.extra['mcp_config_path'] is required to build a Claude command out of a run"
        )
        return build_claude_command(
            model=spec.model,
            max_turns=spec.max_turns,
            mcp_config_path=mcp_config_path,
            mcp=spec.profile.mcp,
            executable=spec.executable or "claude",
        )

    def child_environment(self, spec: RunSpec, environ: Mapping[str, str]) -> dict[str, str] | None:
        return dict(spec.environment) if spec.environment is not None else None

    def prepare_home(self, spec: RunSpec) -> Path | None:
        return None

    def tool_call_completed(self, spec: RunSpec) -> bool:
        if spec.profile.mcp is None or spec.raw_log is None:
            return False
        return tool_call_completed(spec.raw_log)

    def run(self, spec: RunSpec) -> RunResult:
        assert spec.raw_log is not None, "RunSpec.raw_log is required for Claude"
        raw_log = spec.raw_log
        start = time.monotonic()
        exit_code = run_claude(
            prompt=spec.prompt,
            model=spec.model,
            max_turns=spec.max_turns,
            timeout_seconds=spec.timeout_seconds,
            raw_log=raw_log,
            mcp=spec.profile.mcp,
            environment=spec.environment,
            executable=spec.executable or "claude",
            deadline=spec.deadline,
        )
        duration = time.monotonic() - start
        return RunResult(
            exit_code=exit_code,
            provider=self.name,
            model=spec.model,
            report_path=raw_log,
            events_log=raw_log,
            tokens=None,
            duration_seconds=duration,
            tool_call_completed=spec.profile.mcp is not None and tool_call_completed(raw_log),
        )
