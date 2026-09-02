"""Isolated ``codex exec`` adapter for one Dream phase.

The Dream orchestrator owns phase policy.  This module owns the process and
capability boundary: one ChatGPT-authenticated Codex turn, one exact Brain MCP
tool allowlist, separate logs, and a hard process-group timeout.
"""

from __future__ import annotations

import argparse
import json
import os
import subprocess
import sys
import tempfile
from collections.abc import Mapping, Sequence
from pathlib import Path

from brain_v42.mcp.dream_capabilities import (
    DREAM_PHASE_TOOL_ALLOWLISTS,
    DreamCapabilityConfigurationError,
    dream_phase_tool_allowlist,
)
from scripts.dream._agent_capability import (
    CAPABILITY_CONFIGURATION_ERROR as _CAPABILITY_CONFIGURATION_ERROR,
)
from scripts.dream._agent_capability import (
    DEFAULT_MCP_URL as _DEFAULT_MCP_URL,
)
from scripts.dream._agent_capability import (
    MCP_TOKEN_ENV as _MCP_TOKEN_ENV,
)
from scripts.dream._agent_capability import (
    PROVIDER_FALLBACK_EXIT_CODE,
    TIMEOUT_EXIT_CODE,
    build_child_environment,
    preflight_capabilities,
    terminate_process_group,
)

PHASE_TOOL_ALLOWLISTS = DREAM_PHASE_TOOL_ALLOWLISTS

_REASONING_EFFORTS = frozenset({"none", "minimal", "low", "medium", "high", "xhigh"})
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
    # Disabling it bought no isolation — it failed the dispatch closed and cost
    # 60 phases out of 60 on 2026-08-17.  The bound that holds is js_repl_tools_only
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
        # Code mode dispatches tool calls from a JS REPL.  Keep that REPL bounded
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
    proves NOTHING — but neither does it prove that we wrote, so the switchover
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


def _build_arg_parser() -> argparse.ArgumentParser:
    parser = argparse.ArgumentParser(description="Run one isolated Dream phase with Codex")
    parser.add_argument("--preflight-capabilities", action="store_true")
    parser.add_argument("--project-key")
    parser.add_argument("--phase", choices=tuple(PHASE_TOOL_ALLOWLISTS))
    parser.add_argument("--model")
    parser.add_argument("--reasoning-effort", choices=tuple(_REASONING_EFFORTS))
    parser.add_argument("--timeout-seconds", type=float)
    parser.add_argument("--report-log", type=Path)
    parser.add_argument("--events-log", type=Path)
    parser.add_argument("--stderr-log", type=Path)
    parser.add_argument("--workspace", type=Path)
    parser.add_argument(
        "--codex-executable", default=os.environ.get("BRAIN_DREAM_CODEX_BIN", "codex")
    )
    return parser


def main(argv: Sequence[str] | None = None) -> int:
    parser = _build_arg_parser()
    args = parser.parse_args(argv)
    if args.preflight_capabilities:
        if args.project_key is None:
            parser.error("--project-key is required with --preflight-capabilities")
        try:
            _preflight_capabilities(args.project_key, os.environ)
        except DreamCapabilityConfigurationError:
            print(_CAPABILITY_CONFIGURATION_ERROR, file=sys.stderr)
            return 1
        return 0

    required_arguments = {
        "--phase": args.phase,
        "--model": args.model,
        "--reasoning-effort": args.reasoning_effort,
        "--timeout-seconds": args.timeout_seconds,
        "--report-log": args.report_log,
        "--events-log": args.events_log,
        "--stderr-log": args.stderr_log,
    }
    missing = [name for name, value in required_arguments.items() if value is None]
    if missing:
        parser.error(f"the following arguments are required: {', '.join(missing)}")

    prompt = sys.stdin.read()
    if not prompt.strip():
        print("Dream Codex prompt is empty", file=sys.stderr)
        return 1
    return run_codex(
        prompt=prompt,
        phase=args.phase,
        project_key=args.project_key,
        model=args.model,
        reasoning_effort=args.reasoning_effort,
        timeout_seconds=args.timeout_seconds,
        report_log=args.report_log,
        events_log=args.events_log,
        stderr_log=args.stderr_log,
        codex_executable=args.codex_executable,
        workspace=args.workspace,
    )


if __name__ == "__main__":
    raise SystemExit(main())
