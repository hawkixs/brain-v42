"""An isolated ``opencode`` adapter (``opencode run``, the opencode.ai CLI).

Everything below was measured on opencode 1.18.30 (2026-09-15), not read
from its documentation; the binary's own strings were the oracle for the
environment variables it honours.

THE CONFIG TRAVELS INLINE. ``OPENCODE_CONFIG_CONTENT`` carries one JSON
document per invocation, read before any file, so no global config is
touched and two concurrent runs never fight over one. The bearer travels
through the ENVIRONMENT: opencode substitutes ``{env:VAR}`` inside its
config, headers included, so the config names the variable and never holds
the value. This is the rail that writes NO secret to disk.

THE TOOL WALL IS AN ALLOWLIST. The config's ``tools`` map accepts globs:
``{"*": false, "<server>_<tool>": true, ...}`` removes every built-in tool
(bash, edit, write, read, glob, grep, webfetch, task, ...) BEFORE the model
sees them -- measured: asked to list its tools, the model names the scoped
MCP tools and nothing else, and reports bash as ABSENT, not denied. An
allowlist, deliberately: a deny-list would silently admit whatever tool the
next opencode release adds. ``permission`` denies the known machine tools
as a second layer; the wall is ``tools``.

STILL AN EPHEMERAL HOME, for two reasons. opencode persists every session
(prompt, tool outputs) into ``~/.local/share/opencode/opencode.db``, which
must die with the run and never land on persistent disk. And it reads its
subscription credentials from ``~/.local/share/opencode/auth.json``, which
the profile's credentials symlink in from the real HOME.

THE COST NOBODY DOCUMENTS. On a fresh HOME, opencode runs ``bun install``
against registry.npmjs.org into ``~/.config/opencode/node_modules`` -- 63 MiB
of packages plus a 90 MiB npm cache, measured, even under ``--pure`` and
``OPENCODE_DISABLE_DEFAULT_PLUGINS``. A nightly rail cannot depend on npm at
06:00. The adapter therefore symlinks the real HOME's ``node_modules`` and
the two package files into the ephemeral HOME; opencode finds them satisfied
and installs nothing (footprint 936 KiB). A real HOME WITHOUT them refuses
the run rather than let it reach the network: the operator seeds them by
running opencode once by hand. The limit of that guarantee, to read before
feeling protected: the check is PRESENCE, not freshness. An opencode upgrade
whose dependency manifest changed would make the next run install THROUGH
the symlink -- into the operator's ``~/.config/opencode``, from npm -- until
the operator runs the new binary by hand once. Nothing here can tell a stale
manifest from a fresh one without knowing opencode's own versioning.

``--auto`` approves what the config does not explicitly deny; with every
built-in tool removed there is nothing left to approve but MCP calls, which
would otherwise wait for an answer that never comes in headless mode.
``--pure`` loads no external plugin. The prompt goes in ARGV as the
positional message, with stdin closed: the runbook that measured this rail
found ``opencode run`` blocked forever after ``init`` when stdin was a
non-TTY pipe.
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
    TIMEOUT_REPLAYABLE_EXIT_CODE,
    failure_code_after_a_write,
    terminate_process_group,
)
from ..profile import CapabilityProfile, McpServer
from ..result import RunResult, TokenUsage
from ..run_record import answer_text, record, run_id_of
from ..sandbox import ephemeral_root as default_ephemeral_root
from ..sandbox import materialize_credentials
from ..spec import RunSpec

# Kernel limit on a SINGLE argument (MAX_ARG_STRLEN = 32 pages). Beyond it,
# execve returns E2BIG. We keep a margin for the rest of the command line.
MAX_PROMPT_BYTES = 120_000

# What a fresh HOME would otherwise fetch from npm. Symlinked from the real
# HOME; refused when absent there.
RUNTIME_CACHE_PATHS = (
    ".config/opencode/node_modules",
    ".config/opencode/package.json",
    ".config/opencode/package-lock.json",
)

# The built-in tools opencode 1.18.30 names in its permission schema. Denied
# as a second layer; the ``tools`` allowlist is what removes them.
MACHINE_TOOLS = (
    "bash",
    "edit",
    "write",
    "read",
    "patch",
    "glob",
    "grep",
    "list",
    "webfetch",
    "websearch",
    "task",
    "skill",
    "external_directory",
)

# Every switch that keeps a run from reading the operator's world or the
# network: no project ``opencode.json``, no ``~/.claude`` skills or CLAUDE.md,
# no external skills, no plugins, no update check, no models.dev fetch (the
# bundled snapshot answered on 1.18.30), no session sharing, no LSP download.
ISOLATION_ENVIRONMENT: Mapping[str, str] = {
    "OPENCODE_DISABLE_PROJECT_CONFIG": "1",
    "OPENCODE_DISABLE_CLAUDE_CODE": "1",
    "OPENCODE_DISABLE_EXTERNAL_SKILLS": "1",
    "OPENCODE_DISABLE_DEFAULT_PLUGINS": "1",
    "OPENCODE_DISABLE_AUTOUPDATE": "1",
    "OPENCODE_DISABLE_MODELS_FETCH": "1",
    "OPENCODE_DISABLE_SHARE": "1",
    "OPENCODE_DISABLE_LSP_DOWNLOAD": "1",
}


def opencode_config(mcp: McpServer | None) -> dict[str, object]:
    """The inline config of one run: one remote server, an allowlist, no sharing.

    The bearer is referenced as ``{env:<bearer_env_var>}`` whether the profile
    carries the value or not: the value, when given, is exported under that
    variable by :func:`_child_environment`, so the config never holds it.
    """
    tools: dict[str, bool] = {"*": False}
    servers: dict[str, object] = {}
    if mcp is not None:
        servers[mcp.name] = {
            "type": "remote",
            "url": mcp.url,
            "enabled": True,
            "headers": {
                "Authorization": f"Bearer {{env:{mcp.bearer_env_var}}}",
                **dict(mcp.headers),
            },
        }
        if mcp.tools:
            for tool in mcp.tools:
                tools[f"{mcp.name}_{tool}"] = True
        else:
            tools[f"{mcp.name}_*"] = True
    return {
        "$schema": "https://opencode.ai/config.json",
        "share": "disabled",
        "autoupdate": False,
        "mcp": servers,
        "tools": tools,
        "permission": dict.fromkeys(MACHINE_TOOLS, "deny"),
    }


def build_opencode_command(
    *,
    model: str,
    prompt: str,
    home: Path,
    executable: str = "opencode",
    variant: str | None = None,
    title: str | None = None,
) -> list[str]:
    """The headless command line of one run. The prompt is the last argument.

    The model is REQUIRED. Without ``-m`` opencode picks its own default among
    the authenticated providers, and on OpenCode Go that can be a
    ``muse-spark-*-contributor`` model, which Meta trains on: a caller that
    names no model does not get a run.
    """
    if not model.strip():
        raise ValueError("opencode model must not be empty: opencode would pick its own")
    prompt_bytes = len(prompt.encode("utf-8"))
    if prompt_bytes > MAX_PROMPT_BYTES:
        # Refuse BEFORE execve: an E2BIG deep inside a Popen is an opaque
        # OSError, where this names the cause and its size.
        raise ValueError(f"prompt too long for argv: {prompt_bytes} bytes > {MAX_PROMPT_BYTES}")
    command = [executable, "run", "--dir", str(home), "--auto", "--pure", "--format", "json"]
    command.extend(("-m", model))
    if variant and variant.strip():
        command.extend(("--variant", variant))
    if title and title.strip():
        command.extend(("--title", title))
    command.append(prompt)
    return command


def runtime_cache_present(real_home: Path) -> bool:
    return all((real_home / relative).exists() for relative in RUNTIME_CACHE_PATHS)


def build_opencode_home(
    *,
    root: Path,
    name: str,
    profile: CapabilityProfile,
    real_home: Path,
) -> Path:
    """Compose one run's HOME under ``root/name``: borrowed cache, credentials.

    Nothing is WRITTEN: the config is inline and the bearer is in the
    environment. What opencode itself writes there (its session database,
    its logs) disappears with the directory.
    """
    home = root / name
    home.mkdir(parents=True, exist_ok=True)
    home.chmod(0o700)
    for relative in RUNTIME_CACHE_PATHS:
        source = real_home / relative
        target = home / relative
        if source.exists() and not target.exists() and not target.is_symlink():
            target.parent.mkdir(parents=True, exist_ok=True)
            target.symlink_to(source)
    materialize_credentials(home=home, real_home=real_home, credentials=profile.credentials)
    return home


def _events(events_log: Path) -> list[dict[str, object]]:
    if not events_log.is_file():
        return []
    events: list[dict[str, object]] = []
    for raw_line in events_log.read_text(encoding="utf-8", errors="replace").splitlines():
        if not raw_line.strip():
            continue
        try:
            event = json.loads(raw_line)
        except json.JSONDecodeError:
            continue
        if isinstance(event, dict):
            events.append(event)
    return events


def _part(event: Mapping[str, object]) -> Mapping[str, object]:
    part = event.get("part")
    return part if isinstance(part, dict) else {}


def _is_completed_call(event: Mapping[str, object], server: str) -> bool:
    if event.get("type") != "tool_use":
        return False
    part = _part(event)
    tool = part.get("tool")
    state = part.get("state")
    return (
        isinstance(tool, str)
        and tool.startswith(f"{server}_")
        and isinstance(state, dict)
        and state.get("status") == "completed"
    )


def _is_step_start(event: object) -> bool:
    return isinstance(event, dict) and event.get("type") == "step_start"


def _is_call_on_server(event: object, server: str) -> bool:
    if not isinstance(event, dict) or event.get("type") != "tool_use":
        return False
    tool = _part(event).get("tool")
    return isinstance(tool, str) and tool.startswith(f"{server}_")


def tool_call_started(events_log: Path, *, server: str) -> bool:
    """Could a tool call on ``server`` have STARTED in this run?

    The question the runner's own deadline asks, and it is stricter than
    :func:`tool_call_completed`: a call in flight when the process is killed
    may still commit on the server after the kill, so "none succeeded" is not
    enough to replay the run elsewhere.

    Measured on opencode 1.18.30 (``run --format json``, event emitter read
    from the binary): a ``tool_use`` event is written in its TERMINAL state
    only (``completed``/``error``), so a running call leaves no line -- but the
    ``step_start`` of the step that issues it is written first, as it happens,
    through ``process.stdout.write`` inside the event loop. Whether that line
    is ON DISK when the process is killed is what the proof rests on, and it
    was measured (2026-09-20) rather than assumed: Bun 1.3.11 standalone --
    opencode embeds 1.3.14, same line -- writes ``process.stdout.write`` to a
    regular file synchronously; the line survives a SIGKILL 1.5 s later from a
    busy loop that never yields, from a pending ``await``, and from the
    ``for await`` writer loop the emitter uses. Hence the proof is the absence
    of any step: no ``step_start`` and no ``tool_use`` on the server means the
    model never began a turn, and nothing could have been issued. A quota-dead
    link that blocks before its first step (2026-09-19: zero bytes for the
    whole deadline) reads exactly so. Still owed: the same kill on the opencode
    binary itself mid-generation, once the opencode-go quota is back.

    Fail-closed the other way round from :func:`tool_call_completed`: an
    absent or unreadable stream cannot prove the negative, so it answers
    ``True`` ("a call may have started") and refuses the switchover.
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
        if _is_step_start(event) or _is_call_on_server(event, server):
            return True
    return False


def tool_call_completed(events_log: Path, *, server: str) -> bool:
    """Did a tool call on ``server`` SUCCEED anywhere in this event stream?

    An EXACT predicate, and that is what makes it usable as a switchover
    condition: ``False`` proves no mutation was committed there, hence that
    replaying the run on another provider cannot write twice. opencode
    names an MCP tool ``<server>_<tool>``; only a ``completed`` state counts.
    """
    return any(_is_completed_call(event, server) for event in _events(events_log))


def _unwrap_fence(report: str) -> str:
    """Drop a markdown fence that wraps the WHOLE report, and only that one.

    Measured on the canary of 2026-09-15: asked for two report lines,
    glm-5.3-flash answered them inside ``` fences, and the strict validator
    downstream counted four lines. A fence around everything is the model's
    envelope, not its report; a fence INSIDE prose is the report's own
    formatting and is kept as written.
    """
    lines = report.strip("\n").split("\n")
    if len(lines) >= 2 and lines[0].startswith("```") and lines[-1].strip() == "```":
        return "\n".join(lines[1:-1])
    return report


def extract_report(events_log: Path, report_log: Path) -> None:
    """Rebuild the run's report from the ``text`` parts, in order.

    A part may be emitted more than once as it grows: the LAST version of
    each part id wins, at the position of its first appearance. Every part
    is kept, not just the final one, so a machine-readable trailer the agent
    printed in a message of its own survives. A fence around the whole
    report is removed (see :func:`_unwrap_fence`).
    """
    texts: dict[str, str] = {}
    for index, event in enumerate(_events(events_log)):
        if event.get("type") != "text":
            continue
        part = _part(event)
        text = part.get("text")
        if not isinstance(text, str):
            continue
        part_id = part.get("id")
        key = part_id if isinstance(part_id, str) else f"#{index}"
        texts[key] = text
    report = _unwrap_fence("\n\n".join(text for text in texts.values() if text.strip()))
    if report.strip():
        report_log.write_text(report, encoding="utf-8")


def _error_message(event: Mapping[str, object]) -> str:
    error = event.get("error")
    if not isinstance(error, dict):
        return "opencode emitted terminal event: error"
    name = error.get("name")
    data = error.get("data")
    message = data.get("message") if isinstance(data, dict) else None
    return (
        f"opencode emitted terminal event: {name if isinstance(name, str) else 'error'}"
        f"{': ' + message if isinstance(message, str) and message else ''}"
    )


def event_stream_error(
    events_log: Path,
    *,
    server: str | None,
    missing_call_message: str | None = None,
) -> str | None:
    """Return a fail-closed validation error for an opencode JSONL event stream.

    With ``server`` set, a run that completed without one successful call on
    it is an error: a run that was given a server and never used it did not do
    its job, however clean its exit code. ``missing_call_message`` lets a
    caller keep the wording its own logs and tests read for that case.
    """
    if not events_log.is_file():
        return "opencode produced no JSONL event stream"
    finished = False
    completed_server_call = False
    for line_number, raw_line in enumerate(
        events_log.read_text(encoding="utf-8", errors="replace").splitlines(), start=1
    ):
        if not raw_line.strip():
            continue
        try:
            event = json.loads(raw_line)
        except json.JSONDecodeError:
            return f"opencode JSONL line {line_number} is malformed"
        if not isinstance(event, dict):
            return f"opencode JSONL line {line_number} is not an object"
        if event.get("type") == "error":
            return _error_message(event)
        if server is not None and _is_completed_call(event, server):
            completed_server_call = True
        if event.get("type") == "step_finish":
            finished = True
    if not finished:
        return "opencode exited 0 without a step_finish event"
    if server is not None and not completed_server_call:
        return (
            missing_call_message
            or f"opencode completed with no completed MCP tool call on {server}"
        )
    return None


def _int(value: object) -> int:
    return value if isinstance(value, int) and not isinstance(value, bool) else 0


def step_finish_parts(events_log: Path) -> list[Mapping[str, object]]:
    """The ``step_finish`` parts of a stream, ONE per part id, last version wins.

    opencode re-emits a part on every update (``extract_report`` dedupes the
    ``text`` parts for the same reason); counting each emission would double
    every token and cost figure. A part without an id keeps its own slot.
    """
    parts: dict[str, Mapping[str, object]] = {}
    for index, event in enumerate(_events(events_log)):
        if event.get("type") != "step_finish":
            continue
        part = _part(event)
        part_id = part.get("id")
        parts[part_id if isinstance(part_id, str) else f"#{index}"] = part
    return list(parts.values())


def telemetry(events_log: Path) -> tuple[TokenUsage | None, float | None]:
    """Sum every ``step_finish``: fresh input, cached input, output, reasoning, cost.

    ``(None, None)`` when the stream measured nothing, and a ``None`` cost
    when no step carried one: absent is not zero, "measured as free" is the
    one figure that must never be persisted. opencode's ``tokens.input``
    EXCLUDES the cache reads, so ``input`` here is their sum and ``fresh`` is
    what the CLI called input.
    """
    fresh = cached = output = thinking = 0
    cost: float | None = None
    measured = False
    for part in step_finish_parts(events_log):
        tokens = part.get("tokens")
        if not isinstance(tokens, dict):
            continue
        measured = True
        cache = tokens.get("cache")
        fresh += _int(tokens.get("input"))
        cached += _int(cache.get("read")) if isinstance(cache, dict) else 0
        output += _int(tokens.get("output"))
        thinking += _int(tokens.get("reasoning"))
        part_cost = part.get("cost")
        if isinstance(part_cost, (int, float)) and not isinstance(part_cost, bool):
            cost = (cost or 0.0) + float(part_cost)
    if not measured:
        return None, None
    return (
        TokenUsage(
            input=fresh + cached, output=output, fresh=fresh, cached=cached, thinking=thinking
        ),
        cost,
    )


def _failure_exit_code(events_log: Path, default: int, server: str | None) -> int:
    """Translate a failure into "replayable elsewhere" or not, never success."""
    if server is not None and tool_call_completed(events_log, server=server):
        return failure_code_after_a_write(default)
    return PROVIDER_FALLBACK_EXIT_CODE


def _deadline_exit_code(
    events_log: Path, stderr_log: Path, server: str | None, timeout_seconds: float
) -> int:
    """The code of the runner's OWN deadline: 124, or 4 when the stream proves
    no call on ``server`` ever started (see :func:`tool_call_started`). Without
    a server there is nothing a run could have written through, so a hang is
    replayable whatever the stream says. The reading is written to stderr:
    the deadline itself is not the news, the reason it was read as empty is."""
    if server is not None and tool_call_started(events_log, server=server):
        return TIMEOUT_EXIT_CODE
    with stderr_log.open("a", encoding="utf-8") as stderr_stream:
        stderr_stream.write(
            f"opencode reached its deadline ({int(timeout_seconds)} s) with no step started"
            f" and no {server or 'tool'} call in its event stream:"
            " nothing was written, the run is replayable elsewhere\n"
        )
    return TIMEOUT_REPLAYABLE_EXIT_CODE


def _effective_timeout(timeout_seconds: float, deadline: float | None) -> float:
    if deadline is None:
        return timeout_seconds
    return max(0.0, min(timeout_seconds, deadline - time.monotonic()))


def _bearer_value(mcp: McpServer | None, environ: Mapping[str, str]) -> str | None:
    if mcp is None:
        return None
    if mcp.bearer is not None:
        return mcp.bearer.get_secret_value()
    return environ.get(mcp.bearer_env_var) or None


def _child_environment(
    home: Path,
    environ: Mapping[str, str],
    profile: CapabilityProfile,
    *,
    bearer: str | None,
) -> dict[str, str]:
    child = {
        "HOME": str(home),
        "TMPDIR": str(home),
        "PATH": environ.get("PATH", "/usr/bin:/bin"),
        "LANG": environ.get("LANG", "C.UTF-8"),
        "TERM": "dumb",
        **ISOLATION_ENVIRONMENT,
        "OPENCODE_CONFIG_CONTENT": json.dumps(opencode_config(profile.mcp)),
    }
    if profile.mcp is not None and bearer is not None:
        child[profile.mcp.bearer_env_var] = bearer
    for name in profile.environment_passthrough:
        if name in environ and name not in child:
            child[name] = environ[name]
    return child


def run_opencode(
    *,
    prompt: str,
    name: str,
    model: str,
    timeout_seconds: float,
    events_log: Path,
    report_log: Path,
    stderr_log: Path,
    profile: CapabilityProfile,
    real_home: Path | None = None,
    environment: Mapping[str, str] | None = None,
    executable: str = "opencode",
    variant: str | None = None,
    title: str | None = None,
    ephemeral_root: Path | None = None,
    deadline: float | None = None,
    temp_prefix: str = "headless-agents-",
    missing_call_message: str | None = None,
) -> int:
    """Run one opencode invocation and return its code (``124`` on deadline,
    ``4`` on a deadline the stream proves empty, ``3`` if replayable).

    ``environment`` is the AMBIENT environment to read ``PATH``/``LANG``, the
    bearer variable and the profile's passthrough variables from; the child
    gets a rebuilt one whose ``HOME`` is the ephemeral directory. When the
    profile declares a server, the bearer must be visible -- as the profile's
    literal value or under the named variable -- or the run refuses to start,
    so a scoped bearer can never be silently replaced by an ambient one.
    """
    if timeout_seconds <= 0:
        raise ValueError("timeout_seconds must be positive")
    ambient = dict(environment) if environment is not None else dict(os.environ)

    for path in (events_log, report_log, stderr_log):
        path.parent.mkdir(parents=True, exist_ok=True)
    report_log.write_text("", encoding="utf-8")

    source_home = (
        real_home if real_home is not None else Path(ambient.get("HOME", str(Path.home())))
    )
    # Fail-closed BEFORE launching anything: a HOME without the runtime cache
    # would send the run to npm, and a server without a reachable bearer
    # would answer 401 on every call. The two refusals do not carry the same
    # code. The cache is a HOST fact that can flip under a chain (an opencode
    # upgrade, a cleaned HOME): nothing was launched, nothing was written, so
    # the run is provably replayable and says so with ``3``. The bearer is a
    # CONFIGURATION fact: advancing a chain on it would hide a broken
    # registry behind the next link, so it keeps the ``1`` every rail uses.
    if not runtime_cache_present(source_home):
        stderr_log.write_text(
            "opencode runtime cache absent from the real HOME "
            f"({', '.join(RUNTIME_CACHE_PATHS)}): run refused rather than sent to npm; "
            "run opencode once by hand to seed it\n",
            encoding="utf-8",
        )
        return PROVIDER_FALLBACK_EXIT_CODE
    bearer = _bearer_value(profile.mcp, ambient)
    if profile.mcp is not None and bearer is None:
        stderr_log.write_text(
            f"missing required environment variable: {profile.mcp.bearer_env_var}\n",
            encoding="utf-8",
        )
        return 1
    server = profile.mcp.name if profile.mcp is not None else None

    root = ephemeral_root if ephemeral_root is not None else default_ephemeral_root(ambient)

    def _run(base: Path) -> int:
        home = build_opencode_home(root=base, name=name, profile=profile, real_home=source_home)
        try:
            command = build_opencode_command(
                model=model,
                prompt=prompt,
                home=home,
                executable=executable,
                variant=variant,
                title=title,
            )
        except ValueError as exc:
            stderr_log.write_text(f"{exc}\n", encoding="utf-8")
            return PROVIDER_FALLBACK_EXIT_CODE

        # A caller's deadline that has already passed is a TIMEOUT, not a dead
        # link: launching would kill the child at once on an empty stream and
        # read a 4 out of the caller's exhausted budget -- then the next link's,
        # and the next -- so nothing is launched and the plain 124 is returned.
        remaining = _effective_timeout(timeout_seconds, deadline)
        if remaining <= 0:
            stderr_log.write_text(
                "opencode not launched: the caller's deadline had already expired\n",
                encoding="utf-8",
            )
            return TIMEOUT_EXIT_CODE

        with (
            events_log.open("w", encoding="utf-8") as events_stream,
            stderr_log.open("w", encoding="utf-8") as stderr_stream,
        ):
            try:
                process = subprocess.Popen(
                    command,
                    stdin=subprocess.DEVNULL,
                    stdout=events_stream,
                    stderr=stderr_stream,
                    cwd=home,
                    env=_child_environment(home, ambient, profile, bearer=bearer),
                    text=True,
                    start_new_session=True,
                )
            except OSError as exc:
                stderr_stream.write(f"unable to start opencode: {exc}\n")
                return PROVIDER_FALLBACK_EXIT_CODE
            try:
                process.communicate(timeout=remaining)
            except subprocess.TimeoutExpired:
                terminate_process_group(process)
                # A timeout proves nothing BY ITSELF: the run may have written
                # and then hung. The stream decides, after the kill, whether a
                # call could even have started -- see _deadline_exit_code.
                timed_out = True
            else:
                timed_out = False

        if timed_out:
            return _deadline_exit_code(events_log, stderr_log, server, timeout_seconds)

        extract_report(events_log, report_log)
        exit_code = int(process.returncode or 0)
        if exit_code == TIMEOUT_EXIT_CODE:
            return TIMEOUT_EXIT_CODE
        if exit_code != 0:
            return _failure_exit_code(events_log, exit_code, server)
        if not report_log.read_text(encoding="utf-8", errors="replace").strip():
            with stderr_log.open("a", encoding="utf-8") as stderr_stream:
                stderr_stream.write("opencode exited 0 without a final report\n")
            return _failure_exit_code(events_log, 1, server)
        event_error = event_stream_error(
            events_log, server=server, missing_call_message=missing_call_message
        )
        if event_error is not None:
            with stderr_log.open("a", encoding="utf-8") as stderr_stream:
                stderr_stream.write(f"{event_error}\n")
            return _failure_exit_code(events_log, 1, server)
        return 0

    if root is not None:
        with tempfile.TemporaryDirectory(prefix=temp_prefix, dir=str(root)) as temporary:
            return _run(Path(temporary))
    with tempfile.TemporaryDirectory(prefix=temp_prefix) as temporary:
        return _run(Path(temporary))


class OpenCodeProvider:
    """:class:`~headless_agents.protocol.AgentProvider` adapter over opencode.

    ``spec.reasoning_effort`` is passed as opencode's ``--variant`` (the
    provider-specific reasoning effort); ``spec.name`` doubles as the session
    title, which is what ``opencode stats`` and the console show.
    """

    name = "opencode"

    def __init__(
        self, *, real_home: Path | None = None, ephemeral_root: Path | None = None
    ) -> None:
        self._real_home = real_home
        self._ephemeral_root = ephemeral_root

    def _source_home(self, environ: Mapping[str, str]) -> Path:
        if self._real_home is not None:
            return self._real_home
        return Path(environ.get("HOME", str(Path.home())))

    def _root(self, environ: Mapping[str, str]) -> Path:
        if self._ephemeral_root is not None:
            return self._ephemeral_root
        return default_ephemeral_root(environ) or Path(tempfile.gettempdir())

    def _home(self, spec: RunSpec) -> Path:
        environ = spec.environment if spec.environment is not None else os.environ
        return self._root(environ) / spec.name

    def build_command(self, spec: RunSpec) -> list[str]:
        return build_opencode_command(
            model=spec.model,
            prompt=spec.prompt,
            home=self._home(spec),
            executable=spec.executable or "opencode",
            variant=spec.reasoning_effort,
            title=spec.name,
        )

    def child_environment(self, spec: RunSpec, environ: Mapping[str, str]) -> dict[str, str] | None:
        # opencode's child environment is rebuilt around the ephemeral HOME
        # inside run_opencode: the inline config and the bearer live there.
        return None

    def prepare_home(self, spec: RunSpec) -> Path | None:
        environ = spec.environment if spec.environment is not None else os.environ
        return build_opencode_home(
            root=self._root(environ),
            name=spec.name,
            profile=spec.profile,
            real_home=self._source_home(environ),
        )

    def tool_call_completed(self, spec: RunSpec) -> bool:
        if spec.profile.mcp is None or spec.events_log is None:
            return False
        return tool_call_completed(spec.events_log, server=spec.profile.mcp.name)

    def run(self, spec: RunSpec) -> RunResult:
        spec = spec.with_run_dir_defaults()
        assert spec.events_log is not None
        assert spec.report_log is not None
        assert spec.stderr_log is not None
        start = time.monotonic()
        exit_code = run_opencode(
            prompt=spec.prompt,
            name=spec.name,
            model=spec.model,
            timeout_seconds=spec.timeout_seconds,
            events_log=spec.events_log,
            report_log=spec.report_log,
            stderr_log=spec.stderr_log,
            profile=spec.profile,
            real_home=self._real_home,
            environment=spec.environment,
            executable=spec.executable or "opencode",
            variant=spec.reasoning_effort,
            title=spec.name,
            ephemeral_root=self._ephemeral_root,
            deadline=spec.deadline,
        )
        duration = time.monotonic() - start
        tokens, cost = telemetry(spec.events_log)
        return record(
            spec,
            RunResult(
                exit_code=exit_code,
                provider=self.name,
                model=spec.model,
                report_path=spec.report_log,
                events_log=spec.events_log,
                tokens=tokens,
                duration_seconds=duration,
                tool_call_completed=self.tool_call_completed(spec),
                cost_usd=cost,
                # extract_report joined the stream's text parts there.
                text=answer_text(spec.report_log, exit_code=exit_code),
                run_id=run_id_of(spec),
                stderr_log=spec.stderr_log,
            ),
        )
