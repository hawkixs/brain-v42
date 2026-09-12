"""Isolated ``codex exec`` adapter for one Dream phase.

Moved unchanged from ``scripts/dream/codex_runner.py`` (lot 1 of the agent
runtime extraction, Brain ticket c31bad72): the CLI entry point (``argparse``,
``main()``) stays in that file, now a thin shim that imports the functions
below and re-exports the names its existing tests import by name
(``tests/unit/test_dream_codex_runner.py``,
``tests/unit/test_dream_provider_chain.py``). Everything else -- the hardened
non-interactive ``codex exec`` command, the exact per-phase MCP allowlist,
log separation and the wall-clock timeout -- lives here now.
"""

from __future__ import annotations

import json
import os
import subprocess
import tempfile
import time
from collections.abc import Mapping
from pathlib import Path

from brain_v42.mcp.dream_capabilities import (
    DREAM_PHASE_TOOL_ALLOWLISTS,
    DreamCapabilityConfigurationError,
    dream_phase_tool_allowlist,
)

from ..capability import (
    CAPABILITY_CONFIGURATION_ERROR as _CAPABILITY_CONFIGURATION_ERROR,
)
from ..capability import (
    DEFAULT_MCP_URL as _DEFAULT_MCP_URL,
)
from ..capability import (
    MCP_TOKEN_ENV as _MCP_TOKEN_ENV,
)
from ..capability import (
    PROVIDER_FALLBACK_EXIT_CODE,
    TIMEOUT_EXIT_CODE,
    build_child_environment,
    preflight_capabilities,
    terminate_process_group,
)
from ..result import RunResult, TokenUsage
from ..spec import RunSpec

PHASE_TOOL_ALLOWLISTS = DREAM_PHASE_TOOL_ALLOWLISTS

# ``max`` and ``ultra`` are declared by Codex 0.153 for gpt-6-astra and the
# gpt-5.6 family (``~/.codex/models_cache.json``, measured 2026-09-12); the deep
# tier runs Astra at ``max``. A value refused here fails the phase BEFORE launch,
# with no Brain tool call to prove and hence no switchover to the next provider.
_REASONING_EFFORTS = frozenset(
    {"none", "minimal", "low", "medium", "high", "xhigh", "max", "ultra"}
)
# Codex resolves its own state directory from CODEX_HOME; no other rail needs
# it, so it extends the shared base allowlist rather than widening it.
_CODEX_CHILD_ENV_EXTRA = frozenset({"CODEX_HOME"})
_DISABLED_FEATURES = (
    "apps",
    "auth_elicitation",
    "browser_use",
    "browser_use_external",
    "browser_use_full_cdp_access",
    # NOT "code_mode_host": since Codex 0.147.0 the gpt-5.6-* models route every
    # MCP tool call through that host, with no direct surface to fall back to.
    # Disabling it bought no isolation -- it failed the dispatch closed and cost
    # 60 phases out of 60 on 2026-08-17. The bound that holds is js_repl_tools_only
    # below, plus the phase allowlist and the server-side capability scope.
    "computer_use",
    "goals",
    "hooks",
    "image_generation",
    "in_app_browser",
    "memories",
    "multi_agent",
    "plugins",
    "remote_plugin",
    "shell_snapshot",
    "shell_tool",
    "skill_mcp_dependency_install",
    "tool_call_mcp_elicitation",
    "unified_exec",
    "workspace_dependencies",
)


def _codex_child_environment(
    *,
    project_key: str | None,
    phase: str,
    environ: Mapping[str, str],
) -> dict[str, str] | None:
    return build_child_environment(
        project_key=project_key,
        phase=phase,
        environ=environ,
        extra_allowlist=_CODEX_CHILD_ENV_EXTRA,
    )


def _preflight_capabilities(project_key: str, environ: Mapping[str, str]) -> None:
    preflight_capabilities(project_key, environ)


def _write_capability_configuration_error(stderr_log: Path) -> None:
    stderr_log.parent.mkdir(parents=True, exist_ok=True)
    stderr_log.write_text(f"{_CAPABILITY_CONFIGURATION_ERROR}\n", encoding="utf-8")


def _toml(value: object) -> str:
    """Serialize the small TOML value subset used by CLI config overrides."""
    if isinstance(value, dict):
        entries = ",".join(f"{json.dumps(str(key))}={_toml(item)}" for key, item in value.items())
        return f"{{{entries}}}"
    return json.dumps(value, ensure_ascii=True, separators=(",", ":"))


def build_codex_command(
    *,
    phase: str,
    model: str,
    reasoning_effort: str,
    report_log: Path,
    workspace: Path,
    codex_executable: str = "codex",
    mcp_url: str | None = None,
) -> list[str]:
    """Build the hardened non-interactive Codex command for one phase."""
    tools = dream_phase_tool_allowlist(phase)
    if not model.strip():
        raise ValueError("Codex model must not be empty")
    if reasoning_effort not in _REASONING_EFFORTS:
        raise ValueError(f"unsupported Codex reasoning effort: {reasoning_effort}")

    server_url = mcp_url or os.environ.get("BRAIN_DREAM_MCP_URL", _DEFAULT_MCP_URL)
    agent_header = f"dream-codex-{phase}"

    overrides: tuple[tuple[str, object], ...] = (
        ("forced_login_method", "chatgpt"),
        ("approval_policy", "never"),
        ("check_for_update_on_startup", False),
        ("history.persistence", "none"),
        ("model_reasoning_effort", reasoning_effort),
        ("project_doc_max_bytes", 0),
        ("web_search", "disabled"),
        ("apps._default.enabled", False),
        ("memories.use_memories", False),
        ("memories.generate_memories", False),
        # Code mode dispatches tool calls from a JS REPL. Keep that REPL bounded
        # to tool calls so re-enabling the host restores dispatch without handing
        # the phase a general-purpose execution surface.
        ("features.js_repl_tools_only", True),
        ("mcp_servers.brain-v42.url", server_url),
        ("mcp_servers.brain-v42.bearer_token_env_var", _MCP_TOKEN_ENV),
        (
            "mcp_servers.brain-v42.http_headers",
            {
                "X-Brain-Agent": agent_header,
                "X-Brain-Tool-Profile": "native",
            },
        ),
        ("mcp_servers.brain-v42.required", True),
        ("mcp_servers.brain-v42.enabled_tools", list(tools)),
        ("mcp_servers.brain-v42.default_tools_approval_mode", "approve"),
        ("mcp_servers.brain-v42.startup_timeout_sec", 15),
        ("mcp_servers.brain-v42.tool_timeout_sec", 180),
    ) + tuple((f"features.{feature}", False) for feature in _DISABLED_FEATURES)

    command = [
        codex_executable,
        "exec",
        "--ephemeral",
        "--json",
        "--ignore-user-config",
        "--strict-config",
        "--ignore-rules",
        "--skip-git-repo-check",
        "-C",
        str(workspace),
        "--model",
        model,
        "--sandbox",
        "read-only",
    ]
    for key, value in overrides:
        command.extend(("-c", f"{key}={_toml(value)}"))
    command.extend(("--output-last-message", str(report_log), "-"))
    return command


def _failure_exit_code(events_log: Path, default: int) -> int:
    """Translate a failure into "replayable elsewhere" or not, never success."""
    if brain_tool_call_completed(events_log):
        return default
    return PROVIDER_FALLBACK_EXIT_CODE


def brain_tool_call_completed(events_log: Path) -> bool:
    """Did a Brain tool call SUCCEED anywhere in this event stream?

    An EXACT predicate, and that is what makes it usable as a switchover
    condition: `False` proves no mutation was committed, hence that replaying
    the phase on another provider cannot write twice.

    Fail-closed in both directions that matter. An absent or unreadable stream
    proves NOTHING -- but neither does it prove that we wrote, so the switchover
    stays allowed: the night of 2026-08-11 is exactly that case (codex died
    before emitting anything usable). What blocks the switchover is ONLY the
    positive proof of a successful call.

    A call that errored committed nothing; a call to another server committed
    nothing IN Brain. Neither one blocks.
    """
    if not events_log.is_file():
        return False
    for raw_line in events_log.read_text(encoding="utf-8", errors="replace").splitlines():
        if not raw_line.strip():
            continue
        try:
            event = json.loads(raw_line)
        except json.JSONDecodeError:
            continue
        if not isinstance(event, dict) or event.get("type") != "item.completed":
            continue
        item = event.get("item")
        if (
            isinstance(item, dict)
            and item.get("type") == "mcp_tool_call"
            and item.get("server") == "brain-v42"
            and item.get("status") == "completed"
            and item.get("error") is None
        ):
            return True
    return False


def _event_stream_error(events_log: Path) -> str | None:
    """Return a fail-closed validation error for a Codex JSONL event stream."""
    if not events_log.is_file():
        return "Codex produced no JSONL event stream"
    completed = False
    completed_brain_tool_call = False
    for line_number, raw_line in enumerate(
        events_log.read_text(encoding="utf-8", errors="replace").splitlines(), start=1
    ):
        if not raw_line.strip():
            continue
        try:
            event = json.loads(raw_line)
        except json.JSONDecodeError:
            return f"Codex JSONL line {line_number} is malformed"
        if not isinstance(event, dict):
            return f"Codex JSONL line {line_number} is not an object"
        event_type = event.get("type")
        if event_type in {"turn.failed", "error"}:
            return f"Codex emitted terminal event: {event_type}"
        if event_type == "item.completed":
            item = event.get("item")
            if (
                isinstance(item, dict)
                and item.get("type") == "mcp_tool_call"
                and item.get("server") == "brain-v42"
                and item.get("status") == "completed"
                and item.get("error") is None
            ):
                completed_brain_tool_call = True
        if event_type != "turn.completed":
            continue

        usage = event.get("usage")
        if not isinstance(usage, dict):
            return "Codex turn.completed event has no usage object"
        for key in ("input_tokens", "cached_input_tokens", "output_tokens"):
            value = usage.get(key)
            if not isinstance(value, int) or isinstance(value, bool) or value < 0:
                return f"Codex turn.completed usage.{key} is missing or invalid"
        if usage["input_tokens"] <= 0:
            return "Codex turn.completed usage.input_tokens must be positive"
        if usage["output_tokens"] <= 0:
            return "Codex turn.completed usage.output_tokens must be positive"
        if usage["cached_input_tokens"] > usage["input_tokens"]:
            return "Codex cached input exceeds total input"
        completed = True

    if not completed:
        return "Codex exited 0 without a turn.completed event"
    if not completed_brain_tool_call:
        return "Codex completed with no completed Brain MCP tool call"
    return None


def run_codex(
    *,
    prompt: str,
    phase: str,
    project_key: str | None = None,
    model: str,
    reasoning_effort: str,
    timeout_seconds: float,
    report_log: Path,
    events_log: Path,
    stderr_log: Path,
    codex_executable: str = "codex",
    workspace: Path | None = None,
) -> int:
    """Run one Codex phase and return its exit code (``124`` on timeout)."""
    if timeout_seconds <= 0:
        raise ValueError("timeout_seconds must be positive")
    try:
        child_environment = _codex_child_environment(
            project_key=project_key,
            phase=phase,
            environ=os.environ,
        )
    except DreamCapabilityConfigurationError:
        _write_capability_configuration_error(stderr_log)
        return 1
    if child_environment is None and not os.environ.get(_MCP_TOKEN_ENV):
        stderr_log.parent.mkdir(parents=True, exist_ok=True)
        stderr_log.write_text(
            f"missing required environment variable: {_MCP_TOKEN_ENV}\n", encoding="utf-8"
        )
        return 1

    report_log = report_log.resolve()
    events_log = events_log.resolve()
    stderr_log = stderr_log.resolve()
    for path in (report_log, events_log, stderr_log):
        path.parent.mkdir(parents=True, exist_ok=True)
    # Retries reuse stable per-phase paths. Clear the previous final message so
    # an interrupted Codex turn can never be mistaken for a successful retry.
    report_log.write_text("", encoding="utf-8")

    def _run(runtime_dir: Path) -> int:
        runtime_dir.mkdir(parents=True, exist_ok=True)
        command = build_codex_command(
            phase=phase,
            model=model,
            reasoning_effort=reasoning_effort,
            report_log=report_log,
            workspace=runtime_dir,
            codex_executable=codex_executable,
        )
        with (
            events_log.open("w", encoding="utf-8") as events_stream,
            stderr_log.open("w", encoding="utf-8") as stderr_stream,
        ):
            try:
                if child_environment is None:
                    process = subprocess.Popen(
                        command,
                        stdin=subprocess.PIPE,
                        stdout=events_stream,
                        stderr=stderr_stream,
                        cwd=runtime_dir,
                        text=True,
                        start_new_session=True,
                    )
                else:
                    process = subprocess.Popen(
                        command,
                        stdin=subprocess.PIPE,
                        stdout=events_stream,
                        stderr=stderr_stream,
                        cwd=runtime_dir,
                        env=child_environment,
                        text=True,
                        start_new_session=True,
                    )
            except OSError as exc:
                stderr_stream.write(f"unable to start Codex: {exc}\n")
                # Codex did not even start: nothing could have been written.
                return PROVIDER_FALLBACK_EXIT_CODE

            try:
                process.communicate(input=prompt, timeout=timeout_seconds)
            except subprocess.TimeoutExpired:
                terminate_process_group(process)
                # A timeout proves NOTHING: the phase may have written and
                # then hung. Never a switchover here.
                return 124

        if process.returncode != 0:
            child_code = int(process.returncode or 1)
            if child_code == TIMEOUT_EXIT_CODE:
                return TIMEOUT_EXIT_CODE
            return _failure_exit_code(events_log, child_code)
        if (
            not report_log.is_file()
            or not report_log.read_text(encoding="utf-8", errors="replace").strip()
        ):
            with stderr_log.open("a", encoding="utf-8") as stderr_stream:
                stderr_stream.write("Codex exited 0 without a final report\n")
            return _failure_exit_code(events_log, 1)
        event_error = _event_stream_error(events_log)
        if event_error is not None:
            with stderr_log.open("a", encoding="utf-8") as stderr_stream:
                stderr_stream.write(f"{event_error}\n")
            return _failure_exit_code(events_log, 1)
        return 0

    if workspace is not None:
        return _run(workspace.resolve())
    with tempfile.TemporaryDirectory(prefix=f"brain-v42-dream-{phase}-") as temp_dir:
        return _run(Path(temp_dir))


class CodexProvider:
    """:class:`~brain_v42.agents.protocol.AgentProvider` adapter over Codex."""

    name = "codex"

    def build_command(self, spec: RunSpec) -> list[str]:
        assert spec.report_log is not None, "RunSpec.report_log is required for Codex"
        return build_codex_command(
            phase=spec.phase,
            model=spec.model,
            reasoning_effort=spec.reasoning_effort,
            report_log=spec.report_log,
            workspace=spec.workspace or Path.cwd(),
            codex_executable=spec.executable or "codex",
            mcp_url=spec.mcp_url,
        )

    def child_environment(self, spec: RunSpec, environ: Mapping[str, str]) -> dict[str, str] | None:
        return _codex_child_environment(
            project_key=spec.project_key, phase=spec.phase, environ=environ
        )

    def prepare_home(self, spec: RunSpec) -> Path | None:
        return None

    def tool_call_completed(self, events_log: Path) -> bool:
        return brain_tool_call_completed(events_log)

    def run(self, spec: RunSpec) -> RunResult:
        assert spec.report_log is not None
        assert spec.events_log is not None
        assert spec.stderr_log is not None
        start = time.monotonic()
        exit_code = run_codex(
            prompt=spec.prompt,
            phase=spec.phase,
            project_key=spec.project_key,
            model=spec.model,
            reasoning_effort=spec.reasoning_effort,
            timeout_seconds=spec.timeout_seconds,
            report_log=spec.report_log,
            events_log=spec.events_log,
            stderr_log=spec.stderr_log,
            codex_executable=spec.executable or "codex",
            workspace=spec.workspace,
        )
        duration = time.monotonic() - start
        return RunResult(
            exit_code=exit_code,
            provider=self.name,
            model=spec.model,
            report_path=spec.report_log,
            events_log=spec.events_log,
            tokens=self._tokens(spec.events_log),
            duration_seconds=duration,
            tool_call_completed=brain_tool_call_completed(spec.events_log),
        )

    @staticmethod
    def _tokens(events_log: Path) -> TokenUsage | None:
        if not events_log.is_file():
            return None
        for raw_line in events_log.read_text(encoding="utf-8", errors="replace").splitlines():
            if not raw_line.strip():
                continue
            try:
                event = json.loads(raw_line)
            except json.JSONDecodeError:
                continue
            if not isinstance(event, dict) or event.get("type") != "turn.completed":
                continue
            usage = event.get("usage")
            if not isinstance(usage, dict):
                continue
            return TokenUsage(
                fresh=usage.get("input_tokens"),
                cached=usage.get("cached_input_tokens"),
                thinking=usage.get("reasoning_tokens"),
            )
        return None
