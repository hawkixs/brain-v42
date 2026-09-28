"""Isolated ``codex exec`` adapter.

Ported from ``brain_v42.agents.providers.codex`` with the Dream policy
inverted: the MCP server, its bearer variable, its headers and its tool
allowlist arrive as a :class:`~headless_agents.profile.McpServer` (or ``None``
for a run that may reach no server), and the child environment is whatever
the caller built. The hardened non-interactive command line, the stream
validation and the exit-code discipline are unchanged.
"""

from __future__ import annotations

import errno
import fcntl
import hashlib
import json
import os
import pwd
import stat
import subprocess
import tempfile
import time
from collections import Counter
from collections.abc import Mapping
from contextlib import ExitStack
from pathlib import Path
from typing import Any, Final

from ..capability import (
    PROVIDER_FALLBACK_EXIT_CODE,
    TIMEOUT_EXIT_CODE,
    TIMEOUT_REPLAYABLE_EXIT_CODE,
    failure_code_after_a_write,
    terminate_process_group,
)
from ..event_log import read_events
from ..procgroup import preexec_for, spawn_watched
from ..profile import McpServer, Workspace
from ..result import RunResult
from ..run_record import answer_text, record, run_id_of
from ..sandbox import ephemeral_root
from ..spec import RunSpec
from ..structured import is_json_answer, refuse_unsupported, schema_text
from ..workspace import (
    armed_run,
    prepend,
    rail_preamble,
    settle_run,
    workspace_of,
    workspace_summary,
)

# ``max`` and ``ultra`` are declared by Codex 0.153 for gpt-6-astra and the
# gpt-5.6 family. A value refused here fails the run BEFORE launch, with no
# tool call to prove and hence no switchover to the next provider.
REASONING_EFFORTS = frozenset({"none", "minimal", "low", "medium", "high", "xhigh", "max", "ultra"})

# Codex resolves its own state directory from CODEX_HOME; no other rail needs
# it, so it extends the shared base allowlist rather than widening it.
CHILD_ENV_PASSTHROUGH = frozenset({"CODEX_HOME"})

#: The file a schema-constrained run hands ``--output-schema``, inside its own
#: run-owned ``CODEX_HOME`` (see :func:`run_codex`).
OUTPUT_SCHEMA_NAME: Final = "output-schema.json"

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


def _sandbox_mode(workspace_mode: Workspace | None) -> tuple[str, bool]:
    """(``--sandbox`` value, whether the shell tool is on).

    Measured 2026-09-23, see the table in the 0.4.0 lot-2 spec (3.3): the
    shell tool is codex's only way to READ a file, so every workspace run has
    it. ``read-only`` refuses every write the shell attempts; under
    ``workspace-write`` the shell runs inside the same OS sandbox as
    ``apply_patch`` (writes confined to the writable roots, network off), so
    it widens nothing that sandbox did not already allow. Until the pre-tag
    hardening a writable run honoured ``workspace_mode.shell=False`` and was
    left blind -- measured 2026-09-24, it changed nothing -- hence spec
    decision 13: ``Workspace.shell`` does not change codex's command.
    """
    if workspace_mode is None:
        return "read-only", False
    return ("workspace-write" if workspace_mode.write else "read-only"), True


def build_codex_command(
    *,
    model: str,
    reasoning_effort: str,
    report_log: Path,
    workspace: Path,
    mcp: McpServer | None,
    executable: str = "codex",
    workspace_mode: Workspace | None = None,
    ephemeral: bool = True,
    writable_tmp: Path | None = None,
    output_schema: Path | None = None,
) -> list[str]:
    """Build the hardened non-interactive Codex command for one run.

    ``workspace`` is the ``-C`` directory codex runs in -- unchanged from
    before 0.4.0. ``workspace_mode`` is the read/write/shell capability that
    decides the sandbox and the shell tool (see
    :func:`_sandbox_mode`); with ``workspace_mode=None`` every value is
    exactly what it was before this parameter existed.

    ``ephemeral=False`` omits ``--ephemeral`` and changes NOTHING else: the
    production argv always keeps it (Q80's own ephemeral CODEX_HOME already
    isolates every run); only :func:`run_codex`'s confinement-probe entry
    point (``rollout_log`` set) drops it, so codex's session rollout survives
    long enough to be copied out before the run-owned CODEX_HOME is removed
    -- the only place a sandbox refusal is ever recorded (learnings
    a5460289, 80934778: ``codex exec --json`` never logs one).

    ``writable_tmp`` is the one temp root a ``workspace-write`` sandbox may
    write besides the workspace (``run_codex`` passes its per-run scratch);
    other sandboxes ignore it. This applies to the confinement probe too:
    ``run_with_rollout`` still passes its own scratch through, so a probed
    codex's recorded ``turn_context.sandbox_policy`` carries the same
    ``writable_roots`` entry a production write run would (see
    :func:`headless_agents.proofs._matches_write_policy`, which accepts it).

    ``output_schema`` is the file ``--output-schema`` reads, placed right before
    ``--output-last-message`` (0.5.3 lot 1). Measured on codex-cli 0.156.0: the
    final message, still written to ``report_log``, is constrained to that JSON
    Schema. ``None`` leaves every argument exactly as it was.
    """
    if not model.strip():
        raise ValueError("Codex model must not be empty")
    if reasoning_effort not in REASONING_EFFORTS:
        raise ValueError(f"unsupported Codex reasoning effort: {reasoning_effort}")

    sandbox, shell_enabled = _sandbox_mode(workspace_mode)

    overrides: tuple[tuple[str, object], ...] = (
        ("forced_login_method", "chatgpt"),
        ("approval_policy", "never"),
        ("check_for_update_on_startup", False),
        ("history.persistence", "none"),
        ("model_reasoning_effort", reasoning_effort),
        # 0 in EVERY mode: repository instructions travel in the preamble, so
        # a tracked AGENTS.md read natively would reach codex twice.
        ("project_doc_max_bytes", 0),
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
    if sandbox == "workspace-write":
        # Spec 0.5.0 §3.8.0: ``workspace-write`` treats ``/tmp`` and ``$TMPDIR``
        # as writable roots besides the workspace, so a second repository placed
        # there could be written by a write run. Both are closed; the run's own
        # TMPDIR is a per-run scratch directory (``run_codex``).
        overrides += (
            ("sandbox_workspace_write.exclude_slash_tmp", True),
            ("sandbox_workspace_write.exclude_tmpdir_env_var", True),
        )
        if writable_tmp is not None:
            # Ticket 0b3fcdbf: with no writable temp root at all, Python's
            # ``tempfile`` falls back to the current directory -- the worktree
            # -- and a sandboxed ``pytest`` put its basetemp tree there, which
            # the engine's commit then swept. The scratch holds no repository.
            overrides += (("sandbox_workspace_write.writable_roots", [str(writable_tmp)]),)
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
        *(("--ephemeral",) if ephemeral else ()),
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
    if output_schema is not None:
        command.extend(("--output-schema", str(output_schema)))
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


def resolve_real_codex_home(environment: Mapping[str, str] | None) -> Path:
    """The 'real' ``CODEX_HOME`` ``run_codex`` reads ``auth.json`` from for a
    given child environment (never the ephemeral one this provider builds
    per run): ``environment['CODEX_HOME']`` when given, else
    ``Path.home()/.codex``. ``Path.home()`` reads THIS PROCESS's own
    ``$HOME`` -- never ``environment['HOME']`` -- so an ``environment`` dict
    that sets a different ``HOME`` (as a sandboxed-home caller's does) does
    not change the fallback.

    Extracted from what was inline in ``run_codex`` (review round) so a
    caller elsewhere can compute EXACTLY the same path for the exact same
    environment, rather than re-implement the fallback and risk it
    drifting: the live confinement probe's own-store guard
    (``tests/live/headless_agents/test_proofs_live.py``) uses this to know
    which store ``run_codex`` could actually have written auth.json's
    session state to, instead of assuming its own process's ``$CODEX_HOME``
    is the same one.

    Does not validate the result is absolute: ``run_codex`` still refuses a
    relative ``CODEX_HOME`` before any spawn; this function only resolves.
    ``None`` reads ``os.environ`` (this process's own environment).
    """
    visible = environment if environment is not None else os.environ
    codex_home_value = visible.get("CODEX_HOME")
    return (
        Path(codex_home_value).resolve() if codex_home_value else (Path.home() / ".codex").resolve()
    )


def build_codex_home(*, root: Path, real_codex_home: Path) -> Path:
    """The ephemeral ``CODEX_HOME`` a workspace-capability run gets instead of
    the caller's real one: a private ``0700`` directory holding nothing but a
    symlink to the real ``auth.json``.

    Codex resolves its login, its config and its session state from
    ``CODEX_HOME``; handing a sandboxed run the real directory would expose
    the operator's own ``AGENTS.md``, sessions and config to it. Expected:
    ``codex exec`` needs nothing else under ``CODEX_HOME`` to authenticate a
    non-interactive run -- confirmed by the Task 8 live test.
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
    workspace edit. Expected: ``codex exec`` writes an
    ``item.started``/``item.completed`` event with ``item.type`` in
    ``{"command_execution", "file_change"}`` for a shell command and for an
    ``apply_patch`` edit respectively, one JSON line per event -- confirmed by
    the Task 8 live test.

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


#: codex item types that are not tool calls (spec §3.11 excludes messages and
#: reasoning; an ``error`` item is neither a call nor a message -- plan P4).
NON_TOOL_ITEM_TYPES: Final = frozenset({"agent_message", "reasoning", "error"})


def count_tools(events_log: Path | None) -> dict[str, int] | None:
    """Tool calls in this ``--json`` event stream, by item type (spec 0.5.0 §3.11).

    codex writes ``item.started`` then ``item.completed`` for one call, under
    one ``item.id``: a call is counted once, by that id (plan P2) -- one that
    started and never completed included, since it may have run. ``None`` when
    the stream cannot be read whole, or names a tool item with no id: not
    measured, never a partial count.
    """
    events = read_events(events_log)
    if events is None:
        return None
    calls: dict[str, str] = {}
    for event in events:
        item = event.get("item")
        if not isinstance(item, dict):
            continue
        kind = item.get("type")
        if not isinstance(kind, str) or kind in NON_TOOL_ITEM_TYPES:
            continue
        item_id = item.get("id")
        if not isinstance(item_id, str):
            return None
        calls.setdefault(item_id, kind)
    return dict(Counter(calls.values()))


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


# The ephemeral auth.json is codex's own OAuth state, refreshed by an atomic
# replace (temp file + rename): a legitimate rotation is a small JSON object,
# never anything close to this. A bound well past any real token payload,
# not a precise one -- it exists to make an oversized forgery fail fast.
_MAX_ROTATED_AUTH_BYTES = 65536


def _account_id(raw: bytes) -> str | None:
    """``tokens.account_id`` out of an ``auth.json`` payload, or ``None`` when
    the bytes do not parse to that shape. Never raises: a caller comparing
    two of these treats ``None`` as "no account to match", not as an error.
    Deliberately tolerant of non-UTF-8 bytes too (``UnicodeDecodeError``),
    since the real file's own snapshot goes through this with no other
    validation ahead of it -- see ``run_codex``'s snapshot step."""
    try:
        parsed = json.loads(raw)
    except (json.JSONDecodeError, UnicodeDecodeError):
        return None
    if not isinstance(parsed, dict):
        return None
    tokens = parsed.get("tokens")
    if not isinstance(tokens, dict):
        return None
    account_id = tokens.get("account_id")
    return account_id if isinstance(account_id, str) else None


def _read_ephemeral_rotation(ephemeral_auth: Path) -> bytes | None:
    """Read a candidate rotated ``auth.json`` out of the ephemeral home.

    ``None`` means there is nothing to persist -- the symlink is still in
    place, or the child deleted ``auth.json`` outright -- NEITHER is an
    error. Anything else that fails validation raises ``ValueError``: a
    caller must read that as "do not write this back", never as "crash".

    Opened with ``O_NOFOLLOW`` so a symlink is refused at the syscall itself,
    whether it was never replaced or was swapped back in between an earlier
    probe and this read (TOCTOU) -- there is no separate ``is_symlink()``
    check for that race to slip past. Also ``O_NONBLOCK``, so a FIFO swapped
    in for ``auth.json`` cannot block this open (and hence the ``finally``
    that calls it) waiting for a writer that will never come -- the ``fstat``
    below then refuses it on ``S_ISREG`` alone, never attempting a read.
    ``O_CLOEXEC`` so the descriptor never leaks into a later child.
    """
    try:
        descriptor = os.open(
            str(ephemeral_auth),
            os.O_RDONLY | os.O_NOFOLLOW | os.O_NONBLOCK | os.O_CLOEXEC,
        )
    except FileNotFoundError:
        return None
    except OSError as exc:
        if exc.errno == errno.ELOOP:
            # Still (or again) a symlink: nothing rotated, nothing to persist.
            return None
        raise
    try:
        info = os.fstat(descriptor)
        if not stat.S_ISREG(info.st_mode) or info.st_size == 0:
            return None
        if info.st_size > _MAX_ROTATED_AUTH_BYTES:
            raise ValueError(f"exceeds {_MAX_ROTATED_AUTH_BYTES} bytes")
        raw = os.read(descriptor, info.st_size)
    finally:
        os.close(descriptor)
    try:
        parsed = json.loads(raw)
    except (json.JSONDecodeError, UnicodeDecodeError) as exc:
        # A decode failure's own message can carry the offending byte
        # values: never let it past this generic, content-free wording.
        raise ValueError("is not valid JSON") from exc
    if not isinstance(parsed, dict):
        raise ValueError("is not a JSON object")
    return raw


#: How long a write-back waits for another run holding the auth.json lock.
_AUTH_LOCK_SECONDS = 10.0


def _persist_rotated_auth_or_raise(
    *,
    ephemeral_home: Path,
    real_auth_target: Path,
    real_auth_digest_at_build: str | None,
    real_account_id_at_build: str | None,
) -> None:
    # ``None``: the build-time snapshot itself could not be taken (an
    # unreadable or non-UTF-8 real file -- see run_codex) -- no write-back is
    # possible for this run, already logged there; nothing left to do here.
    if real_auth_digest_at_build is None:
        return
    raw = _read_ephemeral_rotation(ephemeral_home / "auth.json")
    if raw is None:
        return
    if real_account_id_at_build is None:
        raise ValueError("real auth.json carries no account_id to match against")
    if _account_id(raw) != real_account_id_at_build:
        raise ValueError("rotated auth.json account_id does not match the real one")
    # The compare and the replace hold one exclusive lock beside the file, so
    # two runs that both rotated cannot both see it unchanged and overwrite
    # each other (codex review of #207, round 3; claude's twin: #206).
    lock = os.open(auth_lock_path(real_auth_target), os.O_RDWR | os.O_CREAT | os.O_CLOEXEC, 0o600)
    try:
        deadline = time.monotonic() + _AUTH_LOCK_SECONDS
        while True:
            try:
                fcntl.flock(lock, fcntl.LOCK_EX | fcntl.LOCK_NB)
                break
            except BlockingIOError:
                if time.monotonic() >= deadline:
                    raise TimeoutError("the auth.json lock stayed busy") from None
                time.sleep(0.05)
        _replace_if_unchanged(real_auth_target, raw, real_auth_digest_at_build)
    finally:
        os.close(lock)


def auth_lock_path(real_auth_target: Path) -> Path:
    """The lock every ``ha`` run takes to write a rotated login back to ``real_auth_target``."""
    return real_auth_target.with_name(f".{real_auth_target.name}.ha-lock")


def _replace_if_unchanged(real_auth_target: Path, raw: bytes, digest_at_build: str) -> None:
    current = real_auth_target.read_bytes() if real_auth_target.is_file() else None
    if current is None or hashlib.sha256(current).hexdigest() != digest_at_build:
        raise ValueError("real auth.json changed since the ephemeral home was built")
    descriptor, temp_name = tempfile.mkstemp(dir=real_auth_target.parent, prefix=".auth.json.")
    try:
        with os.fdopen(descriptor, "wb") as temp_file:
            temp_file.write(raw)
            temp_file.flush()
            os.fsync(temp_file.fileno())
        os.chmod(temp_name, 0o600)
        os.replace(temp_name, real_auth_target)
    except BaseException:
        Path(temp_name).unlink(missing_ok=True)
        raise
    # The rename is durable on the FILE; fsync the directory too, so the
    # directory entry pointing at it survives a crash right after replace.
    dir_descriptor = os.open(str(real_auth_target.parent), os.O_RDONLY)
    try:
        os.fsync(dir_descriptor)
    finally:
        os.close(dir_descriptor)


def _persist_rotated_auth(
    *,
    ephemeral_home: Path,
    real_auth_target: Path,
    real_auth_digest_at_build: str | None,
    real_account_id_at_build: str | None,
    snapshot_failure: str | None,
    stderr_log: Path,
) -> None:
    """Rescue a codex-rotated ``auth.json`` across the ephemeral ``CODEX_HOME``'s
    teardown -- and refuse to launder anything else through it.

    THREAT this defends against: codex may refresh its OAuth token by an
    atomic replace (write a temp file, then rename it over ``auth.json``);
    that rename turns the ephemeral ``auth.json`` SYMLINK into a regular file
    holding the new token. Left alone, tearing down the ephemeral home would
    delete that token with it, leaving the operator's real ``auth.json``
    holding a refresh token codex itself has already rotated past -- revoked
    by the time codex's next real run tries it. But a ``workspace-write``
    sandbox can ALSO replace that same path with bytes of its own choosing:
    this crosses from an adversarial sandbox into the operator's real
    credentials, so the candidate is read with ``O_NOFOLLOW`` (see
    :func:`_read_ephemeral_rotation`), bounded in size, parsed as JSON, and
    accepted only when it carries the SAME ``tokens.account_id`` the real
    file already held (never a different one, and never when the real file
    had none to compare against). A compare-and-swap against the real file's
    digest, snapshotted when the ephemeral home was built, refuses the write
    if the real file moved on meanwhile (a fresh login, or another run's own
    rotation) rather than overwriting it.

    Never raises past this boundary -- catches ``Exception`` itself, not a
    narrower set: this is a best-effort side effect running in a ``finally``,
    and NOTHING it can do -- an ``OSError`` from the filesystem, a
    ``ValueError`` from validation, sandbox-chosen bytes deep enough to blow
    the JSON decoder's own recursion limit (``RecursionError``, a
    ``RuntimeError``, not a ``ValueError``) -- may replace the exit code
    ``_run`` already decided. The ONLY trace any of that leaves is one line
    appended to ``stderr_log``, carrying the exception's CLASS NAME alone,
    never its message: a decode error's own message can quote the offending
    bytes, and a class name never can. Even that append is guarded: an
    ``OSError`` writing the log itself is swallowed, not re-raised -- this
    function must not be able to break the run over its own diagnostics.

    RESIDUAL, deliberately not defended here: the sandbox can still READ the
    real ``auth.json`` through the ephemeral symlink -- this function
    protects the real file's INTEGRITY, not its confidentiality. Spec 3.3
    already accepts that codex reads outside the workspace.

    ``snapshot_failure`` -- the class name of whatever kept ``run_codex``
    from taking the build-time snapshot at all (an unreadable or non-UTF-8
    real file) -- is logged HERE, in this same ``finally``-time append,
    rather than where it was discovered: ``_run`` opens ``stderr_log`` in
    truncating (``"w"``) mode, so anything appended before ``_run`` runs
    would simply be erased by it.
    """
    if snapshot_failure is not None:
        try:
            with stderr_log.open("a", encoding="utf-8") as stderr_stream:
                stderr_stream.write(
                    f"codex auth.json rotation rescue disabled for this run: {snapshot_failure}\n"
                )
        except OSError:
            pass
    try:
        _persist_rotated_auth_or_raise(
            ephemeral_home=ephemeral_home,
            real_auth_target=real_auth_target,
            real_auth_digest_at_build=real_auth_digest_at_build,
            real_account_id_at_build=real_account_id_at_build,
        )
    except Exception as exc:
        try:
            with stderr_log.open("a", encoding="utf-8") as stderr_stream:
                stderr_stream.write(
                    f"codex auth.json rotation not persisted: {type(exc).__name__}\n"
                )
        except OSError:
            pass


# The POSIX-conventional world-writable scratch directory. A module-level
# name, not an inline literal, so a test can monkeypatch it: pytest's own
# ``tmp_path`` fixture lives under the REAL ``/tmp`` on this machine, which
# would make every "here is a safe root" fixture built under ``tmp_path``
# unsafe by this check alone, with no way to construct a counter-example
# otherwise. Production code never overrides it.
_CONVENTIONAL_TMP_ROOT = Path("/tmp")  # nosec B108 - denylist entry, never written to: a CODEX_HOME root under it is REFUSED (see _unsafe_home_roots)


def _unsafe_home_roots(*, environ: Mapping[str, str], workspace_path: Path) -> frozenset[Path]:
    """Every filesystem root a ``workspace-write`` sandbox could plausibly
    write into: a candidate ``CODEX_HOME`` root must not equal, or sit
    under, any of these.

    ``/tmp`` and ``tempfile.gettempdir()`` are the conventional writable
    scratch roots; ``TMPDIR`` is checked in BOTH the parent process's own
    environment and the CHILD environment the run was given, because
    :func:`~headless_agents.sandbox.sandbox_environment` sets a sandboxed
    run's ``HOME`` equal to its ``TMPDIR`` -- a candidate built from the
    child's ``HOME`` alone would otherwise land right back inside the
    sandbox it is meant to be kept out of. ``workspace_path`` -- the ``-C``
    directory itself -- is unsafe too: a write-mode run confined to it can
    write ANYWHERE inside it via ``apply_patch`` or its own shell, not only
    under a conventional tmp root.
    """
    roots = {
        _CONVENTIONAL_TMP_ROOT.resolve(),
        Path(tempfile.gettempdir()).resolve(),
        workspace_path.resolve(),
    }
    for source in (os.environ, environ):
        tmpdir_value = source.get("TMPDIR")
        if tmpdir_value:
            roots.add(Path(tmpdir_value).resolve())
    return frozenset(roots)


def _is_safe_home_root(candidate: Path, unsafe_roots: frozenset[Path]) -> bool:
    resolved = candidate.resolve()
    if resolved in unsafe_roots:
        return False
    return not any(root in resolved.parents for root in unsafe_roots)


def _choose_codex_home_root(environ: Mapping[str, str], *, workspace_path: Path) -> Path | None:
    """Root directory the ephemeral ``CODEX_HOME`` is created under, or
    ``None`` when every candidate sits somewhere a ``workspace-write``
    sandbox could itself write, or when the uid has no passwd entry to
    derive the fallback from -- the caller fails the run closed on that,
    rather than build a ``CODEX_HOME`` the very agent it isolates could
    reach.

    :func:`~headless_agents.sandbox.ephemeral_root` (``XDG_RUNTIME_DIR``, a
    tmpfs) is used when it passes :func:`_is_safe_home_root`. Otherwise, a
    private ``0700`` directory under the OPERATOR's own home --
    ``pwd.getpwuid(os.getuid()).pw_dir``, read from the OS's user database,
    NEVER from an environment variable (unlike ``Path.home()``, which reads
    ``HOME`` -- exactly the value a sandboxed child's own environment can
    set to its writable tmp root, see :func:`_unsafe_home_roots`).
    """
    unsafe_roots = _unsafe_home_roots(environ=environ, workspace_path=workspace_path)
    candidate = ephemeral_root(environ)
    if candidate is not None and _is_safe_home_root(candidate, unsafe_roots):
        return candidate
    try:
        operator_home = Path(pwd.getpwuid(os.getuid()).pw_dir)
    except KeyError:
        # No passwd entry (a container's arbitrary uid): no operator home to
        # fall back on, and ``HOME`` is exactly what must not stand in for it.
        return None
    fallback = operator_home / ".cache" / "headless-agents" / "codex-homes"
    if not _is_safe_home_root(fallback, unsafe_roots):
        return None
    fallback.mkdir(parents=True, exist_ok=True)
    fallback.chmod(0o700)
    return fallback


def _effective_timeout(timeout_seconds: float, deadline: float | None) -> float:
    if deadline is None:
        return timeout_seconds
    return max(0.0, min(timeout_seconds, deadline - time.monotonic()))


#: A rollout codex 0.156.0 was measured to write for a two-call confinement
#: probe session: ~97 KB. Comfortably above that, well below anything that
#: would make copying it out before CODEX_HOME's teardown expensive.
_ROLLOUT_MAX_BYTES: Final = 32 * 1024 * 1024


#: Read chunk size for the capped, looping read below (see ``_read_capped``).
_ROLLOUT_READ_CHUNK_BYTES: Final = 1024 * 1024


def _opendir_nofollow(parent_fd: int, name: str) -> int | None:
    """Open directory ``name`` under the already-open ``parent_fd``, refusing
    to follow a symlink AT THIS STEP -- ``None`` when ``name`` is missing,
    is itself a symlink, or is not a directory. Every level of the walk
    below opens relative to an fd already known good, never by re-resolving
    a string path an attacker could retarget between a check and a use
    (review round, TOCTOU): ``O_NOFOLLOW`` here refuses the symlink at the
    syscall itself, ``O_DIRECTORY`` refuses anything that is not one."""
    try:
        return os.open(name, os.O_RDONLY | os.O_DIRECTORY | os.O_NOFOLLOW, dir_fd=parent_fd)
    except OSError:
        return None


def _open_regular_nofollow(parent_fd: int, name: str) -> int | None:
    """Open file ``name`` under ``parent_fd`` for reading, ``None`` when it
    is missing, a symlink, or not a regular file -- checked on the OPENED
    descriptor's own ``fstat``, never a separate ``stat`` call a swapped-in
    file could race between the check and the open."""
    try:
        descriptor = os.open(name, os.O_RDONLY | os.O_NOFOLLOW, dir_fd=parent_fd)
    except OSError:
        return None
    if not stat.S_ISREG(os.fstat(descriptor).st_mode):
        os.close(descriptor)
        return None
    return descriptor


def _rollout_candidate_descriptors(home: Path) -> list[int]:
    """Open file descriptors of every ``rollout-*.jsonl`` regular file three
    directory levels under ``home/sessions`` (the measured depth, codex
    0.156.0) -- walked with ``dir_fd``-relative opens the whole way down,
    refusing a symlink at EVERY level (unlike ``Path.glob``, which happily
    follows one). The caller closes every descriptor it does not use.
    """
    try:
        root_fd = os.open(str(home), os.O_RDONLY | os.O_DIRECTORY | os.O_NOFOLLOW)
    except OSError:
        return []
    try:
        sessions_fd = _opendir_nofollow(root_fd, "sessions")
    finally:
        os.close(root_fd)
    if sessions_fd is None:
        return []
    matches: list[int] = []
    try:
        for year_entry in list(os.scandir(sessions_fd)):
            year_fd = _opendir_nofollow(sessions_fd, year_entry.name)
            if year_fd is None:
                continue
            try:
                for month_entry in list(os.scandir(year_fd)):
                    month_fd = _opendir_nofollow(year_fd, month_entry.name)
                    if month_fd is None:
                        continue
                    try:
                        for day_entry in list(os.scandir(month_fd)):
                            day_fd = _opendir_nofollow(month_fd, day_entry.name)
                            if day_fd is None:
                                continue
                            try:
                                for file_entry in list(os.scandir(day_fd)):
                                    name = file_entry.name
                                    if not (
                                        name.startswith("rollout-") and name.endswith(".jsonl")
                                    ):
                                        continue
                                    descriptor = _open_regular_nofollow(day_fd, name)
                                    if descriptor is not None:
                                        matches.append(descriptor)
                            finally:
                                os.close(day_fd)
                    finally:
                        os.close(month_fd)
            finally:
                os.close(year_fd)
    finally:
        os.close(sessions_fd)
    return matches


def _read_capped(descriptor: int, max_bytes: int) -> bytes | None:
    """Read from ``descriptor`` up to ``max_bytes`` + 1, looping until EOF.

    A single ``os.read`` can return FEWER bytes than asked even when more
    remain (review round, agy minor): one call is never enough to prove the
    file was read in full. ``None`` when the extra byte is reached -- the
    file is, or grew to be, larger than the cap -- checked on the bytes
    actually read through THIS descriptor, never on an earlier ``stat`` a
    concurrent writer could race past.
    """
    chunks: list[bytes] = []
    total = 0
    budget = max_bytes + 1
    while total < budget:
        chunk = os.read(descriptor, min(budget - total, _ROLLOUT_READ_CHUNK_BYTES))
        if not chunk:
            break
        chunks.append(chunk)
        total += len(chunk)
    if total > max_bytes:
        return None
    return b"".join(chunks)


def _keep_rollout(home: Path, rollout_log: Path) -> str | None:
    """Copy codex's own session rollout out of the run-owned ``home`` into
    ``rollout_log`` (mode ``0600``), before ``home`` is torn down.

    Learnings a5460289 and 80934778: ``codex exec --json`` never logs a
    sandbox-refused command -- the refusal exists only in the session's OWN
    rollout, as a ``custom_tool_call`` named ``exec`` plus its
    ``custom_tool_call_output``. :func:`run_codex`'s confinement-probe entry
    point (``rollout_log`` set, hence ``ephemeral=False``) is the only run
    that writes one at all; this copies the ONE file out so a caller
    (:func:`headless_agents.proofs.refused_attempts`, given ``rail_version``)
    can read it once ``home`` is gone.

    Fail-closed and total: credits nothing it cannot positively identify as
    THIS run's one rollout, and returns why as a short string -- never
    raises past this boundary (its own caller wraps the call in
    ``try/except OSError`` besides, belt and suspenders). ``home / "sessions"``
    must hold EXACTLY one ``<year>/<month>/<day>/rollout-*.jsonl`` file
    (measured depth, codex 0.156.0): zero means this run wrote no session (or
    codex's own layout changed), two or more means this reader cannot tell
    which one is THIS run's -- either way, nothing is kept. Every directory
    level down to the file is opened ``dir_fd``-relative with ``O_NOFOLLOW``
    (:func:`_rollout_candidate_descriptors`): a symlinked intermediate
    directory -- exactly what a sandbox escape could plant in the run's own
    home before this runs -- is never traversed.
    """
    matches = _rollout_candidate_descriptors(home)
    if len(matches) != 1:
        for descriptor in matches:
            os.close(descriptor)
        return f"expected one rollout, found {len(matches)}"
    descriptor = matches[0]
    try:
        raw = _read_capped(descriptor, _ROLLOUT_MAX_BYTES)
    finally:
        os.close(descriptor)
    if raw is None:
        return f"rollout exceeds {_ROLLOUT_MAX_BYTES} bytes"
    rollout_log.write_bytes(raw)
    rollout_log.chmod(0o600)
    return None


#: The longest turn-failure message :func:`run_codex` appends to stderr: an API
#: error is a few hundred characters; the bound keeps a runaway stream out.
_TURN_FAILURE_MAX_CHARS: Final = 2000


def _turn_failure_message(events_log: Path) -> str | None:
    """The last ``turn.failed`` message of a stream -- else its last ``error`` -- on one line.

    Measured on codex-cli 0.156.0 (0.5.3 Task 0): a schema codex's API refuses
    fails the run with exit 1, an empty last message and an EMPTY stderr; the
    API's ``invalid_json_schema`` 400 reaches only the ``--json`` stream, as a
    ``turn.failed`` event. This reads a failed run's stream for its words, it
    does not validate it: a line that is not a JSON object is skipped, and the
    stream is split on newlines only (JSON escapes every one inside a string).
    """
    try:
        stream = events_log.read_text(encoding="utf-8", errors="replace")
    except OSError:
        return None
    failed: str | None = None
    error: str | None = None
    for line in stream.split("\n"):
        try:
            event = json.loads(line)
        except (ValueError, RecursionError):
            continue
        if not isinstance(event, dict):
            continue
        if event.get("type") == "turn.failed":
            detail = event.get("error")
            message = detail.get("message") if isinstance(detail, dict) else None
            if isinstance(message, str) and message.strip():
                failed = message
        elif event.get("type") == "error":
            message = event.get("message")
            if isinstance(message, str) and message.strip():
                error = message
    chosen = failed if failed is not None else error
    if chosen is None:
        return None
    return " ".join(chosen.split())[:_TURN_FAILURE_MAX_CHARS]


def _write_output_schema(home: Path, text: str) -> Path:
    """``text`` into a new ``0600`` :data:`OUTPUT_SCHEMA_NAME` file in ``home``; its path.

    ``O_EXCL``: never over an existing file, never through a link.
    """
    path = home / OUTPUT_SCHEMA_NAME
    descriptor = os.open(path, os.O_WRONLY | os.O_CREAT | os.O_EXCL, 0o600)
    with os.fdopen(descriptor, "w", encoding="utf-8") as stream:
        stream.write(text)
    return path


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
    rollout_log: Path | None = None,
    output_schema: str | None = None,
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

    ``rollout_log`` is the confinement-probe-only escape hatch (learnings
    a5460289, 80934778): when set, the run drops ``--ephemeral`` so codex
    writes its session rollout inside the run-owned ``CODEX_HOME``, any stale
    file at ``rollout_log`` from an earlier attempt in the same run
    directory is removed before spawn, and the one rollout written is copied
    out to it (mode ``0600``) in the existing teardown, after the auth
    rescue, while the ephemeral home still exists (see :func:`_keep_rollout`).
    Never changes the run's own exit code, and never raises: an unkept
    rollout is only ever a ``rollout not kept: <reason>`` line appended to
    ``stderr_log``. ``None`` (the default, and every production call) is
    byte-identical to before this parameter existed.

    ``output_schema`` is the compact JSON of a schema codex's strict mode takes
    (:func:`headless_agents.structured.schema_text`, refused up front otherwise
    by :func:`headless_agents.structured.refuse_unsupported`). It is written to
    :data:`OUTPUT_SCHEMA_NAME` (mode ``0600``) inside the run-owned
    ``CODEX_HOME`` -- outside the workspace, the run directory and the writable
    scratch, and removed with that home on every exit path -- and handed to
    ``--output-schema``. codex's API enforces it; a run that fails under it
    gets the stream's own failure appended to ``stderr_log`` as ``codex turn
    failed: <message>`` (see :func:`_turn_failure_message`), since codex writes
    none there. Whether the final message is JSON is not checked here: the
    provider holds the answer to that (:class:`CodexProvider`). ``None`` leaves
    the run exactly as it was.
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
    if rollout_log is not None:
        rollout_log = rollout_log.resolve()
        # A stale rollout from an earlier attempt in the same run directory
        # must never be mistaken for this run's own evidence.
        rollout_log.unlink(missing_ok=True)

    # Every run gets an ephemeral CODEX_HOME (operator decision Q80 = a): the
    # live isolation proof measured, on codex 0.156.0, that a run on the real
    # one loads the operator's ~/.codex/AGENTS.md despite --ignore-user-config.
    codex_home_value = visible.get("CODEX_HOME")
    if codex_home_value and not Path(codex_home_value).is_absolute():
        stderr_log.write_text(
            f"codex CODEX_HOME must be an absolute path, got: {codex_home_value}\n",
            encoding="utf-8",
        )
        return PROVIDER_FALLBACK_EXIT_CODE
    real_codex_home = resolve_real_codex_home(visible)
    if not (real_codex_home / "auth.json").is_file():
        stderr_log.write_text(
            f"codex auth.json not found under {real_codex_home}\n", encoding="utf-8"
        )
        return PROVIDER_FALLBACK_EXIT_CODE

    def _run(
        runtime_dir: Path,
        run_environment: dict[str, str] | None,
        writable_tmp: Path | None,
        schema_file: Path | None,
    ) -> int:
        runtime_dir.mkdir(parents=True, exist_ok=True)
        command = build_codex_command(
            model=model,
            reasoning_effort=reasoning_effort,
            report_log=report_log,
            workspace=runtime_dir,
            mcp=mcp,
            executable=executable,
            workspace_mode=workspace_capability,
            ephemeral=rollout_log is None,
            writable_tmp=writable_tmp,
            output_schema=schema_file,
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
                process, lifeline = spawn_watched(
                    command,
                    stdin=subprocess.PIPE,
                    stdout=events_stream,
                    stderr=stderr_stream,
                    cwd=runtime_dir,
                    text=True,
                    start_new_session=True,
                    preexec_fn=preexec_for(os.getpid()),
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
            except BaseException:
                # Ctrl-C reaches ha only (the provider has its own session):
                # kill the provider's group before the interruption propagates,
                # or it keeps running -- and writing -- behind ha (spec 0.5.0
                # §3.8.2).
                terminate_process_group(process)
                raise
            else:
                timed_out = False
            finally:
                # Normal end: the watcher leaves without killing (Q75 = a).
                lifeline.release()

        if timed_out:
            return _deadline_exit_code(
                events_log, stderr_log, server, timeout_seconds, workspace_write=workspace_write
            )

        if process.returncode != 0:
            failure = _turn_failure_message(events_log) if schema_file is not None else None
            if failure is not None:
                with stderr_log.open("a", encoding="utf-8") as stderr_stream:
                    stderr_stream.write(f"codex turn failed: {failure}\n")
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

    with ExitStack() as scope:
        if workspace_capability is not None:
            runtime_dir = workspace_capability.path.resolve()
        elif workspace is not None:
            runtime_dir = workspace.resolve()
        else:
            runtime_dir = Path(scope.enter_context(tempfile.TemporaryDirectory(prefix=temp_prefix)))
        # Resolved once, here: the compare-and-swap and the eventual replace
        # both need the file the symlink points AT (a dotfile manager may
        # symlink auth.json elsewhere), not the symlink path itself.
        real_auth_target = (real_codex_home / "auth.json").resolve()
        # An unreadable or non-UTF-8 real file must not break this run: the
        # snapshot (hence the write-back) is simply unavailable for it. The
        # failure is logged from _persist_rotated_auth, in the finally,
        # AFTER _run -- _run itself opens stderr_log in truncating mode, so
        # anything appended here, before _run, would just be erased by it.
        snapshot_failure: str | None = None
        real_auth_snapshot: bytes | None
        try:
            read_snapshot = real_auth_target.read_bytes()
            read_snapshot.decode("utf-8")
        except OSError as exc:
            real_auth_snapshot = None
            snapshot_failure = type(exc).__name__
        except UnicodeDecodeError:
            real_auth_snapshot = None
            snapshot_failure = "UnicodeDecodeError"
        else:
            real_auth_snapshot = read_snapshot
        real_auth_digest_at_build = (
            hashlib.sha256(real_auth_snapshot).hexdigest()
            if real_auth_snapshot is not None
            else None
        )
        real_account_id_at_build = (
            _account_id(real_auth_snapshot) if real_auth_snapshot is not None else None
        )

        home_root = _choose_codex_home_root(visible, workspace_path=runtime_dir)
        if home_root is None:
            stderr_log.write_text(
                "no codex home root outside the sandbox's writable roots\n", encoding="utf-8"
            )
            return PROVIDER_FALLBACK_EXIT_CODE
        codex_home_dir = scope.enter_context(
            tempfile.TemporaryDirectory(prefix=f"{temp_prefix}home-", dir=home_root)
        )
        scratch = scope.enter_context(
            tempfile.TemporaryDirectory(prefix=f"{temp_prefix}tmp-", dir=home_root)
        )
        ephemeral_home = build_codex_home(
            root=Path(codex_home_dir), real_codex_home=real_codex_home
        )
        run_environment = (
            dict(child_environment) if child_environment is not None else dict(os.environ)
        )
        run_environment["CODEX_HOME"] = str(ephemeral_home)
        schema_file = (
            _write_output_schema(ephemeral_home, output_schema)
            if output_schema is not None
            else None
        )
        writable_tmp: Path | None = None
        if workspace_write:
            # Spec 0.5.0 §3.8.0: never the operator's TMPDIR, which may
            # hold repositories; a scratch directory outside the workspace,
            # holding nothing, removed after the run. It is the sandbox's one
            # writable temp root (ticket 0b3fcdbf), named by all three
            # variables so no tool reaches for the operator's TEMP or TMP.
            writable_tmp = Path(scratch)
            for name in ("TMPDIR", "TEMP", "TMP"):
                run_environment[name] = scratch
        try:
            return _run(runtime_dir, run_environment, writable_tmp, schema_file)
        finally:
            # Every exit path -- success, failure, timeout -- must still
            # rescue a rotated token before the ephemeral home is removed.
            _persist_rotated_auth(
                ephemeral_home=ephemeral_home,
                real_auth_target=real_auth_target,
                real_auth_digest_at_build=real_auth_digest_at_build,
                real_account_id_at_build=real_account_id_at_build,
                snapshot_failure=snapshot_failure,
                stderr_log=stderr_log,
            )
            # Confinement-probe only (rollout_log set): keep the session
            # rollout before this ephemeral home is gone. Never changes the
            # exit code above, and never raises: _keep_rollout is total by
            # design, but this catch-all is the actual promise -- a failed
            # copy simply leaves no rollout, which the reader treats as
            # inconclusive (fail-safe), never a crash of the run itself.
            # Broadened from OSError to Exception (review round 2, agy
            # blocker): nothing guarantees a future failure mode here stays
            # an OSError, and this boundary must hold regardless.
            if rollout_log is not None:
                try:
                    reason = _keep_rollout(ephemeral_home, rollout_log)
                except Exception as exc:  # broad on purpose, see docstring above
                    reason = f"{type(exc).__name__}: {exc}"
                if reason is not None:
                    try:
                        with stderr_log.open("a", encoding="utf-8") as stderr_stream:
                            stderr_stream.write(f"rollout not kept: {reason}\n")
                    except OSError:
                        pass


#: What a run's preamble tells codex about its tools, by mode -- keyed the
#: same way :func:`_sandbox_mode` reads a workspace: the shell is on in both
#: modes (spec decision 13), only what the sandbox lets it write differs.
_TOOLS_NOTE = {
    "read": (
        "Read files with your shell (cat, rg, ls): the sandbox allows reads"
        " and refuses writes, so reading is expected and safe."
    ),
    "write": (
        "Read files with your shell (cat, rg, ls) and edit them with apply_patch;"
        " both run inside the same sandbox, network off."
    ),
}


def _preamble_for(spec: RunSpec, workspace: Workspace | None) -> str:
    """The preamble ``run`` launches with, and (implicitly, via its argv-free
    stdin channel) what a caller reading ``build_command`` would expect."""
    if workspace is None:
        tools_note = ""
    else:
        tools_note = _TOOLS_NOTE["write" if workspace.write else "read"]
    return rail_preamble(spec, tools_note=tools_note)


#: What :meth:`CodexProvider.build_command` shows for the schema file: it exists
#: only inside a run's own ``CODEX_HOME``, so a preview names it and writes nothing.
_OUTPUT_SCHEMA_PREVIEW: Final = Path("<run-owned>") / OUTPUT_SCHEMA_NAME


class CodexProvider:
    """:class:`~headless_agents.protocol.AgentProvider` adapter over Codex.

    An output schema (0.5.3 lot 1) goes to ``exec --output-schema``, measured on
    codex-cli 0.156.0. One its strict mode rejects is refused before anything
    exists (:func:`headless_agents.structured.refuse_unsupported`): codex's API
    would refuse it only once the run started. A final message that is not JSON
    under a schema is exit ``1``, never ``0`` -- its text is kept, for a caller
    to see what came back -- and ``output is not JSON: an output schema was
    set`` is appended to the run's stderr.
    """

    name = "codex"

    def build_command(self, spec: RunSpec) -> list[str]:
        refuse_unsupported(self.name, spec.output_schema)
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
            output_schema=_OUTPUT_SCHEMA_PREVIEW if spec.output_schema is not None else None,
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
        return self._run_spec(spec, rollout_log=None)

    def run_with_rollout(self, spec: RunSpec) -> RunResult:
        """Confinement-probe-only entry point (learnings a5460289, 80934778):
        runs codex WITHOUT ``--ephemeral`` so its session rollout survives
        long enough to be copied out to ``run_dir/rollout.jsonl`` before the
        run-owned ``CODEX_HOME`` is torn down -- the only place a sandbox
        refusal of the exec tool is ever recorded (``codex exec --json``
        never logs one). Production argv is unaffected: only THIS method
        drops the flag, and only for its own call; every other caller of
        :meth:`run` is byte-identical to before this method existed.
        """
        spec = spec.with_run_dir_defaults()
        assert spec.events_log is not None, "RunSpec.events_log is required for the rollout probe"
        return self._run_spec(spec, rollout_log=spec.events_log.parent / "rollout.jsonl")

    def _run_spec(self, spec: RunSpec, *, rollout_log: Path | None) -> RunResult:
        refuse_unsupported(self.name, spec.output_schema)
        spec = spec.with_run_dir_defaults()
        assert spec.report_log is not None
        assert spec.events_log is not None
        assert spec.stderr_log is not None
        start = time.monotonic()
        workspace = workspace_of(spec)
        prompt = prepend(_preamble_for(spec, workspace), spec.prompt)
        tripwire = armed_run(workspace)
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
            rollout_log=rollout_log,
            output_schema=(
                schema_text(spec.output_schema) if spec.output_schema is not None else None
            ),
        )
        duration = time.monotonic() - start
        exit_code, git_tampered = settle_run(tripwire, exit_code, spec.stderr_log)
        # --output-last-message: the report holds the final agent message.
        text = answer_text(spec.report_log, exit_code=exit_code)
        if spec.output_schema is not None and exit_code == 0 and not is_json_answer(text):
            # codex's API enforces the schema; ha does not trust that it did.
            with spec.stderr_log.open("a", encoding="utf-8") as stream:
                stream.write("output is not JSON: an output schema was set\n")
            exit_code = 1
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
                text=text,
                run_id=run_id_of(spec),
                stderr_log=spec.stderr_log,
                workspace=workspace_summary(workspace, git_tampered),
                context=None if spec.context is None else tuple(spec.context.to_list()),
            ),
        )
