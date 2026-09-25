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

import fcntl
import json
import os
import subprocess
import tempfile
import time
from collections.abc import Mapping
from contextlib import nullcontext
from pathlib import Path

from ..capability import (
    INVALID_USAGE_EXIT_CODE,
    PROVIDER_FALLBACK_EXIT_CODE,
    TIMEOUT_EXIT_CODE,
    failure_code_after_a_write,
    terminate_process_group,
)
from ..procgroup import preexec_for, spawn_watched
from ..profile import McpServer, Workspace
from ..result import RunResult
from ..run_record import answer_text, record, run_id_of
from ..spec import RunSpec
from ..workspace import (
    argv_prompt_or_refusal,
    armed_run,
    rail_preamble,
    settle_run,
    workspace_of,
    workspace_summary,
)

# The kernel refuses a single argv element at or above ``MAX_ARG_STRLEN``
# (32 pages -- 131072 bytes on the common 4 KiB page size) with E2BIG.
# Measured: ``/bin/true --append-system-prompt`` followed by a 131072-byte
# argument raises ``OSError`` through ``Popen``; 131071 bytes does not. The
# preamble travels as ONE argv element (``--append-system-prompt``), so this
# is the hard ceiling on how much context this rail can carry that way.
MAX_APPEND_SYSTEM_PROMPT_BYTES = 131_071

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
    workspace: Workspace | None = None,
    append_system_prompt: str | None = None,
) -> list[str]:
    """Build the hardened non-interactive Claude command for one run.

    The exact per-run allowlist replaces any ``mcp__<server>__*`` wildcard: the
    wildcard is what a scoped bearer exists to make unnecessary, and leaving it
    would make the bearer the only line of defence. No server, no
    ``--allowedTools`` at all.

    ``workspace=None`` runs in ``bypassPermissions`` with **no built-in tool**
    (``--tools ""``): only the MCP tools ``--allowedTools`` names are callable.
    A workspace narrows both the
    permission mode and the tool list to what its ``write``/``shell`` flags
    allow, and adds ``--restricted`` (file tools confined to the working
    directory; user, project and local settings ignored -- a trusted
    repository's own ``.claude/settings.json`` cannot widen what this run may
    do, measured). ``shell`` adds ``Bash`` to both lists, but unlike the file
    tools it is NOT confined by ``--restricted``: a shell runs with the
    operator's own user rights, per spec 3.3 -- that confinement, if any, is
    the caller's to provide.
    """
    if not model.strip():
        raise ValueError("Claude model must not be empty")
    if max_turns <= 0:
        raise ValueError("max_turns must be positive")

    if workspace is None:
        permission_mode, tools = "bypassPermissions", ""
    else:
        tool_list = (
            ["Read", "Edit", "Write", "Glob", "Grep"]
            if workspace.write
            else ["Read", "Glob", "Grep"]
        )
        if workspace.shell:
            tool_list.append("Bash")
        permission_mode = "acceptEdits" if workspace.write else "dontAsk"
        tools = ",".join(tool_list)

    command = [executable, "-p", "-", "--model", model, "--max-turns", str(max_turns)]
    if workspace is not None:
        command.append("--restricted")
    command.extend(("--permission-mode", permission_mode, "--tools", tools))
    allowed = [f"mcp__{mcp.name}__{tool}" for tool in mcp.tools] if mcp is not None else []
    if workspace is not None and workspace.shell:
        allowed.append("Bash")
    if allowed:
        command.extend(("--allowedTools", ",".join(allowed)))
    if append_system_prompt:
        command.extend(("--append-system-prompt", append_system_prompt))
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
    for otel_record in content.split('body: "claude_code.tool_result"')[1:]:
        window = otel_record[:2000]
        if 'tool_name: "mcp_tool"' in window and 'success: "true"' in window:
            return True
    return False


def _effective_timeout(timeout_seconds: float, deadline: float | None) -> float:
    if deadline is None:
        return timeout_seconds
    return max(0.0, min(timeout_seconds, deadline - time.monotonic()))


#: The one entry of ``.credentials.json`` a run may see: the Claude login.
#: The file also holds the operator's MCP OAuth tokens (Gmail, Drive, ...),
#: which no agent run needs and none may read.
CLAUDE_OAUTH_KEY = "claudeAiOauth"
_CREDENTIALS_FILE = ".credentials.json"
_MAX_CREDENTIALS_BYTES = 1 << 20
_CREDENTIALS_LOCK_SECONDS = 10.0
_API_KEY_VARIABLES = ("ANTHROPIC_API_KEY", "ANTHROPIC_AUTH_TOKEN")


def _real_config_dir(visible: Mapping[str, str]) -> Path:
    """Where the operator's claude keeps its configuration and credentials."""
    value = visible.get("CLAUDE_CONFIG_DIR", "")
    if value and os.path.isabs(value):
        return Path(value)
    home = visible.get("HOME") or os.environ.get("HOME") or str(Path.home())
    return Path(home) / ".claude"


def _oauth_entry(raw: bytes) -> dict[str, object] | None:
    """The ``claudeAiOauth`` entry of a credentials document, or ``None``."""
    if len(raw) > _MAX_CREDENTIALS_BYTES:
        return None
    try:
        document = json.loads(raw.decode("utf-8"))
    except (UnicodeDecodeError, ValueError):
        return None
    entry = document.get(CLAUDE_OAUTH_KEY) if isinstance(document, dict) else None
    if not isinstance(entry, dict):
        return None
    if not all(isinstance(entry.get(key), str) for key in ("accessToken", "refreshToken")):
        return None
    return entry


def _write_private(path: Path, data: bytes) -> None:
    """``data`` into ``path`` atomically, ``0600``, the directory fsynced."""
    descriptor, temp_name = tempfile.mkstemp(dir=path.parent, prefix=f".{path.name}.")
    try:
        with os.fdopen(descriptor, "wb") as stream:
            stream.write(data)
            stream.flush()
            os.fsync(stream.fileno())
        os.chmod(temp_name, 0o600)
        os.replace(temp_name, path)
    except BaseException:
        Path(temp_name).unlink(missing_ok=True)
        raise
    directory = os.open(str(path.parent), os.O_RDONLY)
    try:
        os.fsync(directory)
    finally:
        os.close(directory)


def _persist_rotated_oauth(
    *,
    ephemeral: Path,
    real: Path,
    real_at_build: bytes,
    copied: dict[str, object],
    raw_log: Path,
) -> None:
    """Write a token claude rotated during the run back to the real file.

    Only the ``claudeAiOauth`` entry is replaced, the other entries kept; and
    only when the real file is byte-identical to what the run copied from --
    another session may have rotated it meanwhile, and its token must win.
    """

    def note(message: str) -> None:
        with raw_log.open("a", encoding="utf-8") as stream:
            stream.write(f"claude credentials: {message}\n")

    try:
        raw = ephemeral.read_bytes()
    except OSError:
        return
    entry = _oauth_entry(raw)
    if entry is None:
        note("the run left an unreadable credentials file; not written back")
        return
    if entry == copied:
        return
    # The compare and the replace hold one exclusive lock beside the file, so
    # two runs that both rotated cannot both see it "unchanged" and overwrite
    # each other (codex review of PR #206, round 4).
    lock = os.open(credentials_lock_path(real), os.O_RDWR | os.O_CREAT | os.O_CLOEXEC, 0o600)
    try:
        deadline = time.monotonic() + _CREDENTIALS_LOCK_SECONDS
        while True:
            try:
                fcntl.flock(lock, fcntl.LOCK_EX | fcntl.LOCK_NB)
                break
            except BlockingIOError:
                if time.monotonic() >= deadline:
                    note(
                        "rotated during the run but not written back: the credentials lock stayed busy"
                    )
                    return
                time.sleep(0.05)
        try:
            current = real.read_bytes()
        except OSError:
            note("rotated during the run but not written back: the real file is gone")
            return
        if current != real_at_build:
            note("rotated during the run but not written back: the real file changed meanwhile")
            return
        document = json.loads(current.decode("utf-8"))
        document[CLAUDE_OAUTH_KEY] = entry
        _write_private(real, json.dumps(document).encode("utf-8"))
    finally:
        os.close(lock)


def credentials_lock_path(real: Path) -> Path:
    """The lock every ``ha`` run takes to write a rotated login back to ``real``."""
    return real.with_name(f".{real.name}.ha-lock")


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
    answer_log: Path | None = None,
    workspace: Workspace | None = None,
    append_system_prompt: str | None = None,
) -> int:
    """Run one Claude invocation and return its exit code (``124`` on timeout).

    stdout and stderr land MIXED in ``raw_log``: the OTEL console stream a
    telemetry splitter consumes travels on stderr alongside the answer, and
    separating them here would silently deprive that consumer of it.

    ``answer_log`` is the one exception, for a caller that wants the answer
    ALONE: stdout goes there (truncated first), while stderr -- the OTEL
    console stream, any CLI warning -- stays in ``raw_log``, where the
    telemetry consumer and :func:`tool_call_completed` still find it.
    Without it, nothing changes.
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
    if answer_log is not None:
        answer_log = answer_log.resolve()
        answer_log.parent.mkdir(parents=True, exist_ok=True)

    # Spec 0.5.0 §3.8.0 (G7), operator decision Q68=a: no run inherits the
    # operator's claude configuration -- CLAUDE.md, skills, plugins, hooks,
    # settings, user MCP servers. The child gets a per-run HOME and
    # CLAUDE_CONFIG_DIR holding nothing but a copy of the Claude login.
    # ``--safe-mode`` would also drop the run's own --mcp-config server
    # (measured on claude 2.1.282, Brain learning 5ffb9e1b).
    real_credentials = _real_config_dir(visible) / _CREDENTIALS_FILE
    try:
        real_at_build: bytes | None = real_credentials.read_bytes()
    except OSError:
        real_at_build = None
    copied = _oauth_entry(real_at_build) if real_at_build is not None else None
    if copied is None and not any(visible.get(name) for name in _API_KEY_VARIABLES):
        with raw_log.open("a", encoding="utf-8") as stream:
            stream.write(
                f"no Claude credentials: {real_credentials} holds no usable "
                f"{CLAUDE_OAUTH_KEY} entry and no API key variable is set\n"
            )
        return PROVIDER_FALLBACK_EXIT_CODE

    with tempfile.TemporaryDirectory(prefix=temp_prefix) as temp_dir:
        runtime_dir = Path(temp_dir)
        isolated_home = runtime_dir / "home"
        isolated_config = runtime_dir / "config"
        isolated_home.mkdir(mode=0o700)
        isolated_config.mkdir(mode=0o700)
        isolated_credentials = isolated_config / _CREDENTIALS_FILE
        if copied is not None:
            descriptor = os.open(isolated_credentials, os.O_WRONLY | os.O_CREAT | os.O_EXCL, 0o600)
            with os.fdopen(descriptor, "w", encoding="utf-8") as stream:
                json.dump({CLAUDE_OAUTH_KEY: copied}, stream)
        run_environment = dict(child_environment if child_environment is not None else os.environ)
        run_environment["HOME"] = str(isolated_home)
        run_environment["CLAUDE_CONFIG_DIR"] = str(isolated_config)
        mcp_config_path = runtime_dir / "mcp-config.json"
        mcp_config_path.write_text(json.dumps(build_claude_mcp_config(mcp)), encoding="utf-8")
        command = build_claude_command(
            model=model,
            max_turns=max_turns,
            mcp_config_path=mcp_config_path,
            mcp=mcp,
            executable=executable,
            workspace=workspace,
            append_system_prompt=append_system_prompt,
        )
        # The temp dir stays the cwd when there is no workspace (unchanged);
        # a workspace becomes the cwd so relative paths in the agent's own
        # tool calls resolve inside it.
        cwd = workspace.path if workspace is not None else runtime_dir

        answer_context = (
            answer_log.open("w", encoding="utf-8") if answer_log is not None else nullcontext()
        )
        with raw_log.open("a", encoding="utf-8") as raw_stream, answer_context as answer_stream:
            try:
                process, lifeline = spawn_watched(
                    command,
                    stdin=subprocess.PIPE,
                    stdout=raw_stream if answer_stream is None else answer_stream,
                    stderr=subprocess.STDOUT if answer_stream is None else raw_stream,
                    cwd=cwd,
                    env=run_environment,
                    text=True,
                    start_new_session=True,
                    preexec_fn=preexec_for(os.getpid()),
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
            except BaseException:
                # Ctrl-C reaches ha only (the provider has its own session):
                # kill the provider's group before the interruption propagates,
                # or it keeps running -- and writing -- behind ha (spec 0.5.0
                # §3.8.2).
                terminate_process_group(process)
                raise
            finally:
                lifeline.release()
                # Every exit path -- success, failure, timeout, interruption --
                # rescues a rotated login before the per-run config is removed.
                if copied is not None and real_at_build is not None:
                    _persist_rotated_oauth(
                        ephemeral=isolated_credentials,
                        real=real_credentials,
                        real_at_build=real_at_build,
                        copied=copied,
                        raw_log=raw_log,
                    )

    exit_code = int(process.returncode or 0)
    if exit_code == 0:
        return 0
    if exit_code == TIMEOUT_EXIT_CODE:
        return TIMEOUT_EXIT_CODE
    if workspace is not None and workspace.write:
        # claude cannot prove that no edit happened (asynchronous OTEL): once
        # the process ran in a writable workspace, nothing is replayable.
        return failure_code_after_a_write(exit_code)
    # The claude rail cannot prove the absence of a write other than through
    # its telemetry: without the OTEL_* variables in the child environment,
    # raw_log holds no tool event at all and the predicate returns False. That
    # is the right default -- it allows the switchover on a rail that plainly
    # did nothing -- and it is also why those variables sit in
    # CHILD_ENV_PASSTHROUGH.
    if tool_call_completed(raw_log):
        return failure_code_after_a_write(exit_code)
    return PROVIDER_FALLBACK_EXIT_CODE


#: What a run's preamble tells the agent about its tools, by mode. Keyed on
#: whether the workspace is writable -- the only distinction a preamble needs,
#: since ``workspace_of`` already resolved read/write/shell into one object.
_TOOLS_NOTE = {
    "read": "Use Read, Glob and Grep to explore it; you cannot edit.",
    "write": "Use Read, Glob, Grep, Edit and Write; edit only what the task needs.",
}


def _preamble_for(spec: RunSpec, workspace: Workspace | None) -> str:
    """The preamble ``run`` launches with, and ``build_command`` previews.

    Factored so the two never drift: a caller inspecting ``build_command``'s
    output must see the tools-note ``run`` actually used, not a second
    computation of the same read/write mode that could disagree with it.
    """
    mode = "write" if workspace is not None and workspace.write else "read"
    return rail_preamble(spec, tools_note=_TOOLS_NOTE[mode])


class ClaudeProvider:
    """:class:`~headless_agents.protocol.AgentProvider` adapter over Claude."""

    name = "claude"

    def build_command(self, spec: RunSpec) -> list[str]:
        mcp_config_path = spec.extra.get("mcp_config_path")
        assert isinstance(mcp_config_path, Path), (
            "RunSpec.extra['mcp_config_path'] is required to build a Claude command out of a run"
        )
        workspace = workspace_of(spec)
        preamble = _preamble_for(spec, workspace)
        return build_claude_command(
            model=spec.model,
            max_turns=spec.max_turns,
            mcp_config_path=mcp_config_path,
            mcp=spec.profile.mcp,
            executable=spec.executable or "claude",
            workspace=workspace,
            append_system_prompt=preamble or None,
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
        spec = spec.with_run_dir_defaults()
        assert spec.raw_log is not None, "RunSpec.raw_log (or run_dir) is required for Claude"
        raw_log = spec.raw_log
        # With a report_log -- named, or given by run_dir -- stdout ALONE is the
        # answer and lands there; stderr (the OTEL console stream, any CLI
        # warning) stays in raw_log. Without one, run_claude APPENDS both to
        # raw_log.
        answer_log = spec.report_log
        workspace = workspace_of(spec)
        preamble = _preamble_for(spec, workspace)
        refusal = argv_prompt_or_refusal(preamble, MAX_APPEND_SYSTEM_PROMPT_BYTES)
        if refusal is not None:
            # Refuse BEFORE execve: a preamble this big would blow past the
            # kernel's per-argument ARG_MAX and Popen would raise OSError deep
            # inside run_claude, read there as "Claude did not start" and
            # answered with PROVIDER_FALLBACK_EXIT_CODE -- a SILENT switchover
            # on a run that never had a chance to run. This is a usage error
            # instead, named and never replayed.
            raw_log.parent.mkdir(parents=True, exist_ok=True)
            with raw_log.open("a", encoding="utf-8") as stream:
                stream.write(f"{refusal}\n")
            return record(
                spec,
                RunResult(
                    exit_code=INVALID_USAGE_EXIT_CODE,
                    provider=self.name,
                    model=spec.model,
                    report_path=answer_log if answer_log is not None else raw_log,
                    events_log=raw_log,
                    tokens=None,
                    duration_seconds=0.0,
                    tool_call_completed=False,
                    text=None,
                    run_id=run_id_of(spec),
                    raw_log=raw_log,
                    workspace=workspace_summary(workspace),
                    context=None if spec.context is None else tuple(spec.context.to_list()),
                ),
            )
        # Remember where this run starts, so an answer read from raw_log
        # never includes what an earlier run left in a reused log.
        offset = raw_log.stat().st_size if raw_log.is_file() else 0
        start = time.monotonic()
        tripwire = armed_run(workspace)
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
            answer_log=answer_log,
            workspace=workspace,
            append_system_prompt=preamble or None,
        )
        duration = time.monotonic() - start
        # claude has no separate stderr log: its stderr lands in raw_log.
        exit_code, git_tampered = settle_run(tripwire, exit_code, raw_log)
        # This rail requests no JSON envelope: the text is read as written.
        if answer_log is not None:
            text = answer_text(answer_log, exit_code=exit_code)
        else:
            text = answer_text(raw_log, exit_code=exit_code, offset=offset)
        return record(
            spec,
            RunResult(
                exit_code=exit_code,
                provider=self.name,
                model=spec.model,
                report_path=answer_log if answer_log is not None else raw_log,
                events_log=raw_log,
                tokens=None,
                duration_seconds=duration,
                tool_call_completed=spec.profile.mcp is not None and tool_call_completed(raw_log),
                text=text,
                run_id=run_id_of(spec),
                raw_log=raw_log,
                workspace=workspace_summary(workspace, git_tampered),
                context=None if spec.context is None else tuple(spec.context.to_list()),
            ),
        )
