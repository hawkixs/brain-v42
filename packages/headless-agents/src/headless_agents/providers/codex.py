"""Isolated ``codex exec`` adapter.

Ported from ``brain_v42.agents.providers.codex`` with the Dream policy
inverted: the MCP server, its bearer variable, its headers and its tool
allowlist arrive as a :class:`~headless_agents.profile.McpServer` (or ``None``
for a run that may reach no server), and the child environment is whatever
the caller built. The hardened non-interactive command line, the stream
validation and the exit-code discipline are unchanged.
"""

from __future__ import annotations

import json
import os
import subprocess
import tempfile
import time
from collections.abc import Mapping
from pathlib import Path
from typing import Any

from ..capability import (
    PROVIDER_FALLBACK_EXIT_CODE,
    TIMEOUT_EXIT_CODE,
    TIMEOUT_REPLAYABLE_EXIT_CODE,
    failure_code_after_a_write,
    terminate_process_group,
)
from ..profile import McpServer, Workspace
from ..result import RunResult
from ..run_record import answer_text, record, run_id_of
from ..sandbox import ephemeral_root
from ..spec import RunSpec
from ..workspace import prepend, rail_preamble, workspace_of, workspace_summary

# ``max`` and ``ultra`` are declared by Codex 0.153 for gpt-6-astra and the
# gpt-5.6 family. A value refused here fails the run BEFORE launch, with no
# tool call to prove and hence no switchover to the next provider.
REASONING_EFFORTS = frozenset({"none", "minimal", "low", "medium", "high", "xhigh", "max", "ultra"})

# Codex resolves its own state directory from CODEX_HOME; no other rail needs
# it, so it extends the shared base allowlist rather than widening it.
CHILD_ENV_PASSTHROUGH = frozenset({"CODEX_HOME"})

_DISABLED_FEATURES = (
    "apps",
    "auth_elicitation",
    "browser_use",
    "browser_use_external",
    "browser_use_full_cdp_access",
    # NOT "code_mode_host": since Codex 0.147.0 the gpt-5.6-* models route every
    # MCP tool call through that host, with no direct surface to fall back to.
    # Disabling it bought no isolation -- it failed the dispatch closed. The
    # bound that holds is js_repl_tools_only below, plus the allowlist and the
    # server-side scope of the bearer.
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


def _toml(value: object) -> str:
    """Serialize the small TOML value subset used by CLI config overrides."""
    if isinstance(value, dict):
        entries = ",".join(f"{json.dumps(str(key))}={_toml(item)}" for key, item in value.items())
        return f"{{{entries}}}"
    return json.dumps(value, ensure_ascii=True, separators=(",", ":"))


def _server_overrides(mcp: McpServer) -> tuple[tuple[str, object], ...]:
    prefix = f"mcp_servers.{mcp.name}"
    return (
        (f"{prefix}.url", mcp.url),
        (f"{prefix}.bearer_token_env_var", mcp.bearer_env_var),
        (f"{prefix}.http_headers", dict(mcp.headers)),
        (f"{prefix}.required", True),
        (f"{prefix}.enabled_tools", list(mcp.tools)),
        (f"{prefix}.default_tools_approval_mode", "approve"),
        (f"{prefix}.startup_timeout_sec", 15),
        (f"{prefix}.tool_timeout_sec", 180),
    )


def _sandbox_mode(workspace_mode: Workspace | None) -> tuple[str, bool, int]:
    """(``--sandbox`` value, whether the shell tool is on, ``project_doc_max_bytes``).

    Measured 2026-09-23, see the table in the 0.4.0 lot-2 spec (3.3):
    ``read-only`` turns the shell tool ON even though ``workspace_mode.write``
    is ``False`` -- it is codex's only way to READ a file, and the
    ``read-only`` sandbox refuses every write it attempts, so enabling it costs
    no isolation. ``workspace-write`` leaves the shell tool OFF unless
    ``workspace_mode.shell`` asks for it: codex edits through ``apply_patch``,
    not the shell, so ``shell=False`` must mean no shell.
    """
    if workspace_mode is None:
        return "read-only", False, 0
    if not workspace_mode.write:
        return "read-only", True, 0
    return "workspace-write", workspace_mode.shell, 65536


def build_codex_command(
    *,
    model: str,
    reasoning_effort: str,
    report_log: Path,
    workspace: Path,
    mcp: McpServer | None,
    executable: str = "codex",
    workspace_mode: Workspace | None = None,
) -> list[str]:
    """Build the hardened non-interactive Codex command for one run.

    ``workspace`` is the ``-C`` directory codex runs in -- unchanged from
    before 0.4.0. ``workspace_mode`` is the read/write/shell capability that
    decides the sandbox, the shell tool and the project-doc budget (see
    :func:`_sandbox_mode`); with ``workspace_mode=None`` every value is
    exactly what it was before this parameter existed.
    """
    if not model.strip():
        raise ValueError("Codex model must not be empty")
    if reasoning_effort not in REASONING_EFFORTS:
        raise ValueError(f"unsupported Codex reasoning effort: {reasoning_effort}")

    sandbox, shell_enabled, doc_max_bytes = _sandbox_mode(workspace_mode)

    overrides: tuple[tuple[str, object], ...] = (
        ("forced_login_method", "chatgpt"),
        ("approval_policy", "never"),
        ("check_for_update_on_startup", False),
        ("history.persistence", "none"),
        ("model_reasoning_effort", reasoning_effort),
        ("project_doc_max_bytes", doc_max_bytes),
        ("web_search", "disabled"),
        ("apps._default.enabled", False),
        ("memories.use_memories", False),
        ("memories.generate_memories", False),
        # Code mode dispatches tool calls from a JS REPL. Keep that REPL bounded
        # to tool calls so re-enabling the host restores dispatch without handing
        # the run a general-purpose execution surface.
        ("features.js_repl_tools_only", True),
    )
    if mcp is not None:
        overrides += _server_overrides(mcp)
    # ``features.shell_tool`` must be emitted exactly once: codex's ``-c``
    # last-wins behaviour is unmeasured, so the disabled-feature loop and the
    # enabling branch below are mutually exclusive, never both.
    disabled_features = (
        tuple(feature for feature in _DISABLED_FEATURES if feature != "shell_tool")
        if shell_enabled
        else _DISABLED_FEATURES
    )
    overrides += tuple((f"features.{feature}", False) for feature in disabled_features)
    if shell_enabled:
        overrides += (("features.shell_tool", True),)

    command = [
        executable,
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
        sandbox,
    ]
    for key, value in overrides:
        command.extend(("-c", f"{key}={_toml(value)}"))
    command.extend(("--output-last-message", str(report_log), "-"))
    return command


def _is_completed_call(event: object, server: str) -> bool:
    if not isinstance(event, dict) or event.get("type") != "item.completed":
        return False
    item = event.get("item")
    return (
        isinstance(item, dict)
        and item.get("type") == "mcp_tool_call"
        and item.get("server") == server
        and item.get("status") == "completed"
        and item.get("error") is None
    )


def _is_call_on_server(event: object, server: str) -> bool:
    if not isinstance(event, dict) or event.get("type") not in ("item.started", "item.completed"):
        return False
    item = event.get("item")
    return (
        isinstance(item, dict)
        and item.get("type") == "mcp_tool_call"
        and item.get("server") == server
    )


def tool_call_started(events_log: Path, *, server: str) -> bool:
    """Could a tool call on ``server`` have STARTED in this run?

    The question the runner's own deadline asks, stricter than
    :func:`tool_call_completed`: a call in flight when the process is killed
    may still commit on the server after the kill.

    Measured on the live stream (2026-09-20): Codex writes an ``item.started``
    for an ``mcp_tool_call`` (``status: in_progress``) BEFORE the call
    executes, one JSON line per event. A stream with no ``mcp_tool_call`` item
    on the server, in any state, therefore shows a run that never issued one
    -- a turn still streaming its message, or a link that never answered.

    Fail-closed the other way round from :func:`tool_call_completed`: an
    absent or unreadable stream cannot prove the negative and answers
    ``True``.
    """
    if not events_log.is_file():
        return True
    try:
        raw_lines = events_log.read_text(encoding="utf-8", errors="replace").splitlines()
    except OSError:
        return True
    for raw_line in raw_lines:
        if not raw_line.strip():
            continue
        try:
            event = json.loads(raw_line)
        except json.JSONDecodeError:
            return True
        if _is_call_on_server(event, server):
            return True
    return False


def tool_call_completed(events_log: Path, *, server: str) -> bool:
    """Did a tool call on ``server`` SUCCEED anywhere in this event stream?

    An EXACT predicate, and that is what makes it usable as a switchover
    condition: ``False`` proves no mutation was committed there, hence that
    replaying the run on another provider cannot write twice.

    Fail-closed in both directions that matter. An absent or unreadable stream
    proves NOTHING -- but neither does it prove that we wrote, so the switchover
    stays allowed. What blocks the switchover is ONLY the positive proof of a
    successful call. A call that errored committed nothing; a call to another
    server committed nothing HERE. Neither one blocks.
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
        if _is_completed_call(event, server):
            return True
    return False


def event_stream_error(
    events_log: Path,
    *,
    server: str | None,
    missing_call_message: str | None = None,
) -> str | None:
    """Return a fail-closed validation error for a Codex JSONL event stream.

    With ``server`` set, a run that completed without one successful call on
    it is an error: a run that was given a server and never used it did not do
    its job, however clean its exit code. ``missing_call_message`` lets a
    caller keep the wording its own logs and tests read for that case.
    """
    if not events_log.is_file():
        return "Codex produced no JSONL event stream"
    completed = False
    completed_server_call = False
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
        if server is not None and _is_completed_call(event, server):
            completed_server_call = True
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
    if server is not None and not completed_server_call:
        return (
            missing_call_message or f"Codex completed with no completed MCP tool call on {server}"
        )
    return None


def build_codex_home(*, root: Path, real_codex_home: Path) -> Path:
    """The ephemeral ``CODEX_HOME`` a workspace-capability run gets instead of
    the caller's real one: a private ``0700`` directory holding nothing but a
    symlink to the real ``auth.json``.

    Codex resolves its login, its config and its session state from
    ``CODEX_HOME``; handing a sandboxed run the real directory would expose
    the operator's own ``AGENTS.md``, sessions and config to it. Measured
    2026-09-23: ``codex exec`` needs nothing else under ``CODEX_HOME`` to
    authenticate a non-interactive run.
    """
    root.mkdir(parents=True, exist_ok=True)
    root.chmod(0o700)
    (root / "auth.json").symlink_to(real_codex_home / "auth.json")
    return root


def write_tool_started(events_log: Path) -> bool:
    """Could a write-capable tool (a shell command, an ``apply_patch`` edit)
    have STARTED in this run?

    The write-mode twin of :func:`tool_call_started`, for a run that carries
    no MCP server to read a taint from: codex has its own event types for a
    workspace edit. Measured 2026-09-23: ``codex exec`` writes an
    ``item.started``/``item.completed`` event with ``item.type`` in
    ``{"command_execution", "file_change"}`` for a shell command and for an
    ``apply_patch`` edit respectively, one JSON line per event.

    Fail-closed the same way as :func:`tool_call_started`: an absent or
    unreadable stream, or a line that fails to parse, answers ``True`` -- a
    write in flight when the process is killed may still land after the kill.
    """
    if not events_log.is_file():
        return True
    try:
        raw_lines = events_log.read_text(encoding="utf-8", errors="replace").splitlines()
    except OSError:
        return True
    for raw_line in raw_lines:
        if not raw_line.strip():
            continue
        try:
            event = json.loads(raw_line)
        except json.JSONDecodeError:
            return True
        if not isinstance(event, dict) or event.get("type") not in (
            "item.started",
            "item.completed",
        ):
            continue
        item = event.get("item")
        if isinstance(item, dict) and item.get("type") in {"command_execution", "file_change"}:
            return True
    return False


def _deadline_exit_code(
    events_log: Path,
    stderr_log: Path,
    server: str | None,
    timeout_seconds: float,
    *,
    workspace_write: bool = False,
) -> int:
    """The code of the runner's OWN deadline: 124, or 4 when the stream proves
    no call on ``server`` (nor, in a writable workspace, a write) ever
    started. Without a server or a writable workspace there is nothing a run
    could have written through, so a hang is replayable whatever the stream
    says."""
    if server is not None and tool_call_started(events_log, server=server):
        return TIMEOUT_EXIT_CODE
    if workspace_write and write_tool_started(events_log):
        return TIMEOUT_EXIT_CODE
    with stderr_log.open("a", encoding="utf-8") as stderr_stream:
        stderr_stream.write(
            f"Codex reached its deadline ({int(timeout_seconds)} s) with no tool call started"
            f" on {server or 'any server'} in its event stream:"
            " nothing was written, the run is replayable elsewhere\n"
        )
    return TIMEOUT_REPLAYABLE_EXIT_CODE


def _failure_exit_code(
    events_log: Path, default: int, server: str | None, *, workspace_write: bool = False
) -> int:
    """Translate a failure into "replayable elsewhere" or not, never success."""
    if server is not None and tool_call_completed(events_log, server=server):
        return failure_code_after_a_write(default)
    if workspace_write and write_tool_started(events_log):
        return failure_code_after_a_write(default)
    return PROVIDER_FALLBACK_EXIT_CODE


def _effective_timeout(timeout_seconds: float, deadline: float | None) -> float:
    if deadline is None:
        return timeout_seconds
    return max(0.0, min(timeout_seconds, deadline - time.monotonic()))


def run_codex(
    *,
    prompt: str,
    model: str,
    reasoning_effort: str,
    timeout_seconds: float,
    report_log: Path,
    events_log: Path,
    stderr_log: Path,
    mcp: McpServer | None,
    environment: Mapping[str, str] | None = None,
    executable: str = "codex",
    workspace: Path | None = None,
    workspace_capability: Workspace | None = None,
    deadline: float | None = None,
    temp_prefix: str = "headless-agents-codex-",
    missing_call_message: str | None = None,
) -> int:
    """Run one Codex invocation and return its exit code (``124`` on timeout).

    ``environment`` is the child environment; ``None`` inherits this process's.
    When ``mcp`` names a bearer variable, that environment must carry it: the
    run refuses to start otherwise, so a scoped bearer can never be silently
    replaced by an ambient one. ``temp_prefix`` names the throwaway workspace
    when the caller gives none (it is visible in argv, after ``-C``).

    ``workspace_capability`` is the read/write/shell capability (``workspace``
    stays the legacy ``-C`` directory, untouched): when set, its ``path``
    becomes the ``-C`` directory and the run gets an EPHEMERAL ``CODEX_HOME``
    (see :func:`build_codex_home`), torn down whether the run succeeds, fails
    or times out. The real ``CODEX_HOME`` (``environment["CODEX_HOME"]`` or
    ``~/.codex``) must carry an ``auth.json``, or the run is refused before
    any spawn with exit code 3 -- provider unavailable, replayable elsewhere,
    never a switchover that could double a write.
    """
    if timeout_seconds <= 0:
        raise ValueError("timeout_seconds must be positive")
    # ``None`` inherits: Popen is then called WITHOUT ``env`` at all, which is
    # the historical rollback path some callers pin in their tests.
    child_environment = dict(environment) if environment is not None else None
    visible = child_environment if child_environment is not None else os.environ
    if mcp is not None and not visible.get(mcp.bearer_env_var):
        stderr_log.parent.mkdir(parents=True, exist_ok=True)
        stderr_log.write_text(
            f"missing required environment variable: {mcp.bearer_env_var}\n", encoding="utf-8"
        )
        return 1
    server = mcp.name if mcp is not None else None
    workspace_write = workspace_capability is not None and workspace_capability.write

    report_log = report_log.resolve()
    events_log = events_log.resolve()
    stderr_log = stderr_log.resolve()
    for path in (report_log, events_log, stderr_log):
        path.parent.mkdir(parents=True, exist_ok=True)
    # Retries reuse stable per-run paths. Clear the previous final message so
    # an interrupted Codex turn can never be mistaken for a successful retry.
    report_log.write_text("", encoding="utf-8")

    real_codex_home: Path | None = None
    if workspace_capability is not None:
        real_codex_home = Path(visible.get("CODEX_HOME") or str(Path.home() / ".codex"))
        if not (real_codex_home / "auth.json").is_file():
            stderr_log.write_text(
                f"codex auth.json not found under {real_codex_home}\n", encoding="utf-8"
            )
            return PROVIDER_FALLBACK_EXIT_CODE

    def _run(runtime_dir: Path, run_environment: dict[str, str] | None) -> int:
        runtime_dir.mkdir(parents=True, exist_ok=True)
        command = build_codex_command(
            model=model,
            reasoning_effort=reasoning_effort,
            report_log=report_log,
            workspace=runtime_dir,
            mcp=mcp,
            executable=executable,
            workspace_mode=workspace_capability,
        )
        # A caller's deadline that has already passed is a TIMEOUT, not a dead
        # link: launching would kill the child at once on an empty stream and
        # read a 4 out of the caller's exhausted budget -- then the next link's,
        # and the next -- so nothing is launched and the plain 124 is returned.
        remaining = _effective_timeout(timeout_seconds, deadline)
        if remaining <= 0:
            stderr_log.write_text(
                "Codex not launched: the caller's deadline had already expired\n",
                encoding="utf-8",
            )
            return TIMEOUT_EXIT_CODE

        with (
            events_log.open("w", encoding="utf-8") as events_stream,
            stderr_log.open("w", encoding="utf-8") as stderr_stream,
        ):
            popen_kwargs: dict[str, Any] = {}
            if run_environment is not None:
                popen_kwargs["env"] = run_environment
            try:
                process = subprocess.Popen(
                    command,
                    stdin=subprocess.PIPE,
                    stdout=events_stream,
                    stderr=stderr_stream,
                    cwd=runtime_dir,
                    text=True,
                    start_new_session=True,
                    **popen_kwargs,
                )
            except OSError as exc:
                stderr_stream.write(f"unable to start Codex: {exc}\n")
                # Codex did not even start: nothing could have been written.
                return PROVIDER_FALLBACK_EXIT_CODE

            try:
                process.communicate(input=prompt, timeout=remaining)
            except subprocess.TimeoutExpired:
                terminate_process_group(process)
                # A timeout proves nothing BY ITSELF: the run may have written
                # and then hung. The stream decides, after the kill, whether a
                # call could even have started -- see _deadline_exit_code.
                timed_out = True
            else:
                timed_out = False

        if timed_out:
            return _deadline_exit_code(
                events_log, stderr_log, server, timeout_seconds, workspace_write=workspace_write
            )

        if process.returncode != 0:
            child_code = int(process.returncode or 1)
            if child_code == TIMEOUT_EXIT_CODE:
                return TIMEOUT_EXIT_CODE
            return _failure_exit_code(
                events_log, child_code, server, workspace_write=workspace_write
            )
        if (
            not report_log.is_file()
            or not report_log.read_text(encoding="utf-8", errors="replace").strip()
        ):
            with stderr_log.open("a", encoding="utf-8") as stderr_stream:
                stderr_stream.write("Codex exited 0 without a final report\n")
            return _failure_exit_code(events_log, 1, server, workspace_write=workspace_write)
        event_error = event_stream_error(
            events_log, server=server, missing_call_message=missing_call_message
        )
        if event_error is not None:
            with stderr_log.open("a", encoding="utf-8") as stderr_stream:
                stderr_stream.write(f"{event_error}\n")
            return _failure_exit_code(events_log, 1, server, workspace_write=workspace_write)
        return 0

    if workspace_capability is not None:
        assert real_codex_home is not None
        with tempfile.TemporaryDirectory(
            prefix=f"{temp_prefix}home-", dir=ephemeral_root(visible)
        ) as codex_home_dir:
            ephemeral_home = build_codex_home(
                root=Path(codex_home_dir), real_codex_home=real_codex_home
            )
            run_environment = (
                dict(child_environment) if child_environment is not None else dict(os.environ)
            )
            run_environment["CODEX_HOME"] = str(ephemeral_home)
            return _run(workspace_capability.path, run_environment)
    if workspace is not None:
        return _run(workspace.resolve(), child_environment)
    with tempfile.TemporaryDirectory(prefix=temp_prefix) as temp_dir:
        return _run(Path(temp_dir), child_environment)


#: What a run's preamble tells codex about its tools, by mode -- keyed the
#: same way :func:`_sandbox_mode` reads a workspace, since the wording differs
#: by how codex may reach the filesystem, not just by read/write.
_TOOLS_NOTE = {
    "read": (
        "Read files with your shell (cat, rg, ls): the sandbox allows reads"
        " and refuses writes, so reading is expected and safe."
    ),
    "write": "Edit files with apply_patch.",
    "write+shell": "Edit with apply_patch; your shell runs inside the same sandbox, network off.",
}


def _preamble_for(spec: RunSpec, workspace: Workspace | None) -> str:
    """The preamble ``run`` launches with, and (implicitly, via its argv-free
    stdin channel) what a caller reading ``build_command`` would expect."""
    if workspace is None:
        tools_note = ""
    elif not workspace.write:
        tools_note = _TOOLS_NOTE["read"]
    elif workspace.shell:
        tools_note = _TOOLS_NOTE["write+shell"]
    else:
        tools_note = _TOOLS_NOTE["write"]
    return rail_preamble(spec, tools_note=tools_note)


class CodexProvider:
    """:class:`~headless_agents.protocol.AgentProvider` adapter over Codex."""

    name = "codex"

    def build_command(self, spec: RunSpec) -> list[str]:
        assert spec.report_log is not None, "RunSpec.report_log is required for Codex"
        workspace = workspace_of(spec)
        return build_codex_command(
            model=spec.model,
            reasoning_effort=spec.reasoning_effort,
            report_log=spec.report_log,
            workspace=workspace.path if workspace is not None else (spec.workspace or Path.cwd()),
            mcp=spec.profile.mcp,
            executable=spec.executable or "codex",
            workspace_mode=workspace,
        )

    def child_environment(self, spec: RunSpec, environ: Mapping[str, str]) -> dict[str, str] | None:
        return dict(spec.environment) if spec.environment is not None else None

    def prepare_home(self, spec: RunSpec) -> Path | None:
        return None

    def tool_call_completed(self, spec: RunSpec) -> bool:
        if spec.profile.mcp is None or spec.events_log is None:
            return False
        return tool_call_completed(spec.events_log, server=spec.profile.mcp.name)

    def run(self, spec: RunSpec) -> RunResult:
        spec = spec.with_run_dir_defaults()
        assert spec.report_log is not None
        assert spec.events_log is not None
        assert spec.stderr_log is not None
        start = time.monotonic()
        workspace = workspace_of(spec)
        prompt = prepend(_preamble_for(spec, workspace), spec.prompt)
        exit_code = run_codex(
            prompt=prompt,
            model=spec.model,
            reasoning_effort=spec.reasoning_effort,
            timeout_seconds=spec.timeout_seconds,
            report_log=spec.report_log,
            events_log=spec.events_log,
            stderr_log=spec.stderr_log,
            mcp=spec.profile.mcp,
            environment=spec.environment,
            executable=spec.executable or "codex",
            workspace=spec.workspace,
            workspace_capability=workspace,
            deadline=spec.deadline,
        )
        duration = time.monotonic() - start
        server = spec.profile.mcp.name if spec.profile.mcp is not None else None
        return record(
            spec,
            RunResult(
                exit_code=exit_code,
                provider=self.name,
                model=spec.model,
                report_path=spec.report_log,
                events_log=spec.events_log,
                # Not measured here: the envelope module reads turn.completed usage
                # for a caller that wants it. ``None`` means "not measured", never
                # a fabricated zero -- see ``result``'s docstring.
                tokens=None,
                duration_seconds=duration,
                tool_call_completed=(
                    server is not None and tool_call_completed(spec.events_log, server=server)
                ),
                # --output-last-message: the report holds the final agent message.
                text=answer_text(spec.report_log, exit_code=exit_code),
                run_id=run_id_of(spec),
                stderr_log=spec.stderr_log,
                workspace=workspace_summary(workspace),
                context=None if spec.context is None else tuple(spec.context.to_list()),
            ),
        )
