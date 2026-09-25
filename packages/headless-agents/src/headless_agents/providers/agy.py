"""An isolated ``agy`` adapter.

Ported from ``brain_v42.agents.providers.agy`` with the policy inverted: the
MCP server (with a LITERAL bearer), the ``PreToolUse`` guard and the
credential list arrive as a :class:`~headless_agents.profile.CapabilityProfile`;
the ephemeral HOME is composed from it by :mod:`headless_agents.sandbox`.

agy takes NO configuration on the command line: no ``--mcp-config``, no tool
allowlist, no equivalent of claude's ``--tools ""``. Its bundled documentation
knows only two config locations, both global, and a project-level
``.agents/hooks.json`` is NOT discovered -- measured 2026-08-11, in a trusted
workspace and a git repository. Hence the ephemeral HOME: it is the only
route that gives per-invocation control.

TWO PROTECTIONS, TWO PERIMETERS, never to be confused: the guard script, wired
as a ``PreToolUse`` hook, protects the MACHINE; the bearer protects the
CORPUS, and the server is what enforces it. Because the guard is the only wall
between a run and a shell, a profile WITHOUT a guard is refused: there is no
"no tools" agy run other than one whose guard denies every machine tool.

A WORKSPACE run swaps the caller's guard for the package's own
(:mod:`headless_agents.guards.agy_workspace`), copied into the HOME and
probed there before the spawn; a profile carrying both is refused, since the
two do not compose. agy's ``view_file`` cannot list a directory, so the
prompt carries the workspace's file list.

THE RAIL'S ONLY DEVIATION. agy's ``Authorization`` is a literal: its
documentation describes no ``${VAR}`` interpolation. The bearer is therefore
WRITTEN to a file where the other two rails pass it through the environment.
It is confined to a 0700 HOME under a tmpfs by preference -- never persistent
disk -- and destroyed with it.
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
    INVALID_USAGE_EXIT_CODE,
    PROVIDER_FALLBACK_EXIT_CODE,
    TIMEOUT_EXIT_CODE,
    TIMEOUT_REPLAYABLE_EXIT_CODE,
    failure_code_after_a_write,
    terminate_process_group,
)
from ..context import xml_attribute
from ..guards.agy_workspace import GUARD_CONFIG_NAME
from ..procgroup import preexec_for, watch_group
from ..profile import CapabilityProfile, Workspace
from ..result import RunResult
from ..run_record import answer_text, record, run_id_of
from ..sandbox import (
    WORKSPACE_GUARD_NAME,
    build_ephemeral_home,
    refuse_caller_guard_with_workspace,
    refuse_home_under_workspace,
)
from ..sandbox import ephemeral_root as default_ephemeral_root
from ..spec import RunSpec
from ..workspace import (
    argv_prompt_or_refusal,
    armed_run,
    prepend,
    rail_preamble,
    settle_run,
    workspace_of,
    workspace_summary,
)

# Kernel limit on a SINGLE argument (MAX_ARG_STRLEN = 32 pages). Beyond it,
# execve returns E2BIG. We keep a margin for the rest of the command line.
MAX_PROMPT_BYTES = 120_000

# The built-in tools that can change the workspace. ``run_command`` counts:
# with ``shell`` armed its shell is unconfined, and a denied call still shows
# a step, which only errs towards "may have written".
_WRITE_TOOLS = frozenset(
    {"write_to_file", "replace_file_content", "multi_replace_file_content", "run_command"}
)

NO_FILE_LIST = "(not a git repository: no file list)"


def build_agy_command(
    *,
    model: str,
    prompt: str,
    executable: str = "agy",
    timeout_seconds: float = 300.0,
) -> list[str]:
    """The headless command line of one run.

    THE PROMPT GOES IN ARGV, and that is not a choice. Measured 2026-08-11: agy
    IGNORES stdin -- ``--print ""`` with the prompt on stdin returns an empty
    answer, and a prompt in an argument plus context on stdin answers without
    the context. The other two rails deliberately go through stdin to dodge
    ARG_MAX; agy leaves no such option.

    The failure mode if you get it wrong is treacherous: agy answers all the
    same, with a greeting, and the run exits 0 with an off-topic report.

    No secret travels through argv: the bearer lives in the ephemeral HOME's
    ``mcp_config.json``. The prompt is visible there -- instructions and
    context, not a secret.

    ``--dangerously-skip-permissions`` is REQUIRED: without it, agy waits in
    headless mode for an approval that will never come. It is not what bounds
    the run -- that is the ``PreToolUse`` guard, which survives this flag.
    """
    prompt_bytes = len(prompt.encode("utf-8"))
    if prompt_bytes > MAX_PROMPT_BYTES:
        # Refuse BEFORE execve: an E2BIG deep inside a Popen is an opaque
        # OSError, where this names the cause and its size.
        raise ValueError(f"prompt too long for argv: {prompt_bytes} bytes > {MAX_PROMPT_BYTES}")
    command = [
        executable,
        "--print",
        prompt,
        "--output-format",
        "stream-json",
        "--print-timeout",
        f"{int(timeout_seconds)}s",
        "--dangerously-skip-permissions",
        "--disable-slash-commands",
    ]
    if model.strip():
        command.extend(("--model", model))
    return command


def _is_mcp_tool_step(event: object) -> bool:
    if not isinstance(event, dict):
        return False
    step = event.get("step_update")
    return (
        isinstance(step, dict)
        and step.get("step_type") == "tool"
        and step.get("tool_name") == "call_mcp_tool"
    )


def tool_call_started(events_log: Path) -> bool:
    """Could an MCP tool call have STARTED in this stream-json flow?

    The question the runner's own deadline asks, stricter than
    :func:`tool_call_completed`: a call in flight when the process is killed
    may still commit on the server after the kill.

    Measured on the live stream (2026-09-14): agy writes a ``step_update`` of
    ``step_type: tool`` in state ``ACTIVE`` BEFORE the tool executes, then
    ``DONE`` or ``ERROR``. A ``call_mcp_tool`` step in ANY state is therefore
    a call that may have reached the server; a stream without one shows a run
    that never issued one. Built-in tool steps do not count, as in
    :func:`tool_call_completed`: the guard refuses them before anything runs.

    Fail-closed the other way round: an absent or unreadable stream cannot
    prove the negative and answers ``True``.
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
        if _is_mcp_tool_step(event):
            return True
    return False


def write_tool_started(events_log: Path) -> bool:
    """Could a workspace-changing tool have STARTED in this stream-json flow?

    The write-mode twin of :func:`tool_call_started`: any ``step_type: tool``
    step naming one of :data:`_WRITE_TOOLS`, in ANY state -- ``ACTIVE`` is
    written before the tool runs, and a refused one (``ERROR``) counting too
    only errs on the safe side. Fail-closed the same way: an absent or
    unreadable stream answers ``True``.
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
        step = event.get("step_update") if isinstance(event, dict) else None
        if (
            isinstance(step, dict)
            and step.get("step_type") == "tool"
            and step.get("tool_name") in _WRITE_TOOLS
        ):
            return True
    return False


def workspace_listing(root: Path, *, max_entries: int = 2000, max_bytes: int = 32768) -> str:
    """The workspace's files, one per line, as git sees them.

    agy's only read tool, ``view_file``, cannot list a directory: without this
    list the agent would have to guess paths. Tracked plus untracked-not-ignored
    files, capped so a large repository cannot eat the argv the prompt travels
    in. A write run can edit ``.git/config``, so what it could set there is
    overridden: ``core.fsmonitor`` is forced off (a command git would run
    HERE, outside the guard), and the work tree is pinned to ``root`` (a
    ``core.worktree`` pointing elsewhere would list, into the prompt, the
    names of a directory the guard never lets the agent see).

    An entry that could break out of the ``<files>`` block (a newline, a
    carriage return, ``<`` or ``>`` in its name) is dropped and counted with
    the entries the caps left out.
    """
    if (root / ".git").is_dir():
        pinned = ["--git-dir", str(root / ".git"), "--work-tree", str(root)]
    else:
        # A linked worktree's .git is a file naming its git dir elsewhere, so
        # only the work tree is pinned. Measured: ``-c core.worktree=<root>``
        # does NOT beat a per-worktree ``core.worktree``; ``--work-tree`` does.
        pinned = ["--work-tree", str(root)]
    try:
        result = subprocess.run(
            [
                "git",
                "-C",
                str(root),
                *pinned,
                "-c",
                "core.fsmonitor=false",
                "ls-files",
                "--cached",
                "--others",
                "--exclude-standard",
                "-z",
            ],
            capture_output=True,
            timeout=10,
        )
    except (OSError, subprocess.SubprocessError):
        return NO_FILE_LIST
    if result.returncode != 0:
        return NO_FILE_LIST
    entries = [raw.decode("utf-8", errors="replace") for raw in result.stdout.split(b"\0") if raw]
    listed: list[str] = []
    size = 0
    for entry in entries:
        if any(character in entry for character in "\n\r<>"):
            continue
        cost = len(entry.encode("utf-8")) + (1 if listed else 0)
        if len(listed) >= max_entries or size + cost > max_bytes:
            break
        listed.append(entry)
        size += cost
    if len(entries) > len(listed):
        listed.append(f"… {len(entries) - len(listed)} more entries not listed")
    return "\n".join(listed)


def workspace_guard_holds(home: Path, workspace: Workspace) -> bool:
    """PROVE the copied workspace guard confines, the way agy will run it.

    The same stance as :func:`guard_denies_machine_tools`, on the exact file
    in the HOME and under ``HOME=home``, so a broken shebang, a missing config
    or a permissive edit all refuse the run: a read inside is allowed, a read
    of ``/`` is denied, ``run_command`` follows ``workspace.shell``, a read of
    the guard's own config is denied -- whatever put the HOME under the
    guard's root, the agent must not reach the file that draws it -- and, when
    writes are armed, a write to ``.git`` or under it is denied: git runs what
    lands there later, outside any sandbox.
    """
    config_dir = home / ".gemini" / "config"
    guard = config_dir / WORKSPACE_GUARD_NAME
    probes: tuple[tuple[str, dict[str, str], str], ...] = (
        ("view_file", {"AbsolutePath": str(workspace.path)}, "allow"),
        ("view_file", {"AbsolutePath": "/"}, "deny"),
        ("run_command", {"CommandLine": "true"}, "allow" if workspace.shell else "deny"),
        ("view_file", {"AbsolutePath": str(config_dir / GUARD_CONFIG_NAME)}, "deny"),
    )
    if workspace.write:
        probes += tuple(
            ("write_to_file", {"TargetFile": str(workspace.path / target)}, "deny")
            for target in (".git", ".git/hooks/x")
        )
    for tool_name, args, expected in probes:
        payload = json.dumps({"toolCall": {"name": tool_name, "args": args}, "stepIdx": 0})
        try:
            result = subprocess.run(
                [str(guard)],
                input=payload,
                capture_output=True,
                text=True,
                timeout=30,
                env={"HOME": str(home)},
            )
            verdict = json.loads(result.stdout)
        except (OSError, ValueError, subprocess.SubprocessError):
            return False
        if not isinstance(verdict, dict) or verdict.get("decision") != expected:
            return False
    return True


def tool_call_completed(events_log: Path) -> bool:
    """Did an MCP tool call SUCCEED in this stream-json flow?

    ``False`` proves no mutation was committed, hence that replaying the run
    elsewhere carries no risk. Only ``call_mcp_tool`` counts: it is the gateway
    through which agy reaches an MCP server, and the guard refuses everything
    else. A REFUSED ``run_command`` does produce a tool step -- counting it
    would block the switchover on a run that plainly wrote nothing.
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
        if not isinstance(event, dict):
            continue
        step = event.get("step_update")
        if not isinstance(step, dict):
            continue
        if (
            step.get("step_type") == "tool"
            and step.get("state") == "DONE"
            and step.get("tool_name") == "call_mcp_tool"
        ):
            return True
    return False


def guard_denies_machine_tools(guard: Path) -> bool:
    """PROVE the guard refuses, instead of noting that it exists.

    Checking it is present would let through a guard that is empty,
    non-executable, misnamed or made permissive by an edit -- all states in
    which the file exists. So we submit a real payload to it and demand the
    refusal.
    """
    if not guard.is_file():
        return False
    probes = (
        ("run_command", "deny"),
        ("write_to_file", "deny"),
        ("call_mcp_tool", "allow"),
    )
    for tool_name, expected in probes:
        payload = json.dumps({"toolCall": {"name": tool_name, "args": {}}, "stepIdx": 0})
        try:
            result = subprocess.run(
                ["bash", str(guard)],
                input=payload,
                capture_output=True,
                text=True,
                timeout=30,
            )
            decision = json.loads(result.stdout).get("decision")
        except (OSError, ValueError, subprocess.SubprocessError):
            return False
        if decision != expected:
            return False
    return True


def extract_report(events_log: Path, report_log: Path) -> None:
    """Rebuild the run's report from the event stream.

    The final answer lives under ``{"event":"result","result":{"response":...}}``.
    Looking for it elsewhere produces an EMPTY report that a consumer would
    hand to the next step as if it were the answer.
    """
    if not events_log.is_file():
        return
    response = ""
    for raw_line in events_log.read_text(encoding="utf-8", errors="replace").splitlines():
        if not raw_line.strip():
            continue
        try:
            event = json.loads(raw_line)
        except json.JSONDecodeError:
            continue
        if not isinstance(event, dict) or event.get("event") != "result":
            continue
        result = event.get("result")
        if isinstance(result, dict) and isinstance(result.get("response"), str):
            response = result["response"]
    if response.strip():
        report_log.write_text(response, encoding="utf-8")


def _effective_timeout(timeout_seconds: float, deadline: float | None) -> float:
    if deadline is None:
        return timeout_seconds
    return max(0.0, min(timeout_seconds, deadline - time.monotonic()))


def _child_environment(
    home: Path, environ: Mapping[str, str], profile: CapabilityProfile
) -> dict[str, str]:
    child = {
        "HOME": str(home),
        "PATH": environ.get("PATH", "/usr/bin:/bin"),
        "LANG": environ.get("LANG", "C.UTF-8"),
        "TERM": "dumb",
    }
    for name in profile.environment_passthrough:
        if name in environ and name not in child:
            child[name] = environ[name]
    return child


def run_agy(
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
    executable: str = "agy",
    ephemeral_root: Path | None = None,
    deadline: float | None = None,
    temp_prefix: str = "headless-agents-",
    guard_proven: bool = False,
) -> int:
    """Run one agy invocation and return its code (``124`` on deadline, ``3`` if replayable).

    ``environment`` is the AMBIENT environment to read ``PATH``/``LANG`` and
    the profile's passthrough variables from; the child gets a rebuilt one
    whose ``HOME`` is the ephemeral directory. ``real_home`` defaults to the
    ambient ``HOME``; ``ephemeral_root`` to ``XDG_RUNTIME_DIR`` when it exists,
    else the system temporary directory. ``guard_proven=True`` skips the probe
    for a caller that has just run :func:`guard_denies_machine_tools` on the
    same path itself -- the path checks below still apply.

    ``profile.workspace`` runs the agent in that directory under the
    package's own guard instead; ``prompt`` is then expected to carry the
    workspace preamble already (:class:`AgyProvider` builds it).
    """
    workspace = _confined_workspace(profile, profile.workspace)
    if timeout_seconds <= 0:
        raise ValueError("timeout_seconds must be positive")
    ambient = dict(environment) if environment is not None else dict(os.environ)
    root = ephemeral_root if ephemeral_root is not None else default_ephemeral_root(ambient)
    if workspace is not None:
        # Every HOME of this run is created under this root: refuse it here,
        # as ValueError like the caller-guard refusal, before any file exists.
        refuse_home_under_workspace(
            root if root is not None else Path(tempfile.gettempdir()), workspace
        )

    for path in (events_log, report_log, stderr_log):
        path.parent.mkdir(parents=True, exist_ok=True)
    report_log.write_text("", encoding="utf-8")

    # Fail-closed BEFORE launching anything: without a proven guard, an agy
    # run would have a free shell. Refusing to start is the only safe choice,
    # and it is logged. The path must be ABSOLUTE: the probe runs the script
    # from this process's working directory while agy resolves the hook
    # command from the ephemeral HOME, so a relative path can pass the first
    # and name nothing in the second. A workspace run's guard is the
    # package's, probed once it sits in the HOME.
    if workspace is None and (profile.guard is None or not profile.guard.path.is_absolute()):
        stderr_log.write_text(
            "agy tool guard absent or not an absolute path: run refused\n", encoding="utf-8"
        )
        return 1
    if (
        workspace is None
        and profile.guard is not None
        and not guard_proven
        and not guard_denies_machine_tools(profile.guard.path)
    ):
        stderr_log.write_text(
            "agy tool guard absent or permissive: run refused\n", encoding="utf-8"
        )
        return 1
    if profile.mcp is not None and profile.mcp.bearer is None:
        # agy writes the bearer literally into its HOME: a server declared
        # without the value cannot be reached, and the run must say so instead
        # of raising after the guard probe.
        stderr_log.write_text(
            "agy needs the MCP bearer VALUE (McpServer.bearer): run refused\n", encoding="utf-8"
        )
        return 1

    source_home = (
        real_home if real_home is not None else Path(ambient.get("HOME", str(Path.home())))
    )

    def _run(base: Path) -> int:
        home = build_ephemeral_home(
            root=base, name=name, profile=profile, real_home=source_home, workspace=workspace
        )
        if workspace is not None and not workspace_guard_holds(home, workspace):
            stderr_log.write_text(
                "agy workspace guard failed its probe: run refused\n", encoding="utf-8"
            )
            return 1
        try:
            command = build_agy_command(
                model=model,
                prompt=prompt,
                executable=executable,
                timeout_seconds=timeout_seconds,
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
                "agy not launched: the caller's deadline had already expired\n",
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
                    cwd=home if workspace is None else workspace.path,
                    env=_child_environment(home, ambient, profile),
                    text=True,
                    start_new_session=True,
                    preexec_fn=preexec_for(os.getpid()),
                )
            except OSError as exc:
                stderr_stream.write(f"unable to start agy: {exc}\n")
                return PROVIDER_FALLBACK_EXIT_CODE
            lifeline = watch_group(process.pid)
            try:
                process.communicate(timeout=remaining)
            except subprocess.TimeoutExpired:
                terminate_process_group(process)
                # A timeout proves nothing BY ITSELF: the run may have written
                # and then hung. The stream decides, after the kill, whether a
                # call could even have started.
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

        writable = workspace is not None and workspace.write
        if timed_out:
            if tool_call_started(events_log):
                return TIMEOUT_EXIT_CODE
            if writable and write_tool_started(events_log):
                return TIMEOUT_EXIT_CODE
            with stderr_log.open("a", encoding="utf-8") as stderr_stream:
                stderr_stream.write(
                    f"agy reached its deadline ({int(timeout_seconds)} s) with no MCP tool"
                    " step started in its event stream: nothing was written, the run is"
                    " replayable elsewhere\n"
                )
            return TIMEOUT_REPLAYABLE_EXIT_CODE

        extract_report(events_log, report_log)
        exit_code = int(process.returncode or 0)
        if exit_code == 0:
            return 0
        if exit_code == TIMEOUT_EXIT_CODE:
            return TIMEOUT_EXIT_CODE
        if tool_call_completed(events_log):
            return failure_code_after_a_write(exit_code)
        if writable and write_tool_started(events_log):
            return failure_code_after_a_write(exit_code)
        return PROVIDER_FALLBACK_EXIT_CODE

    if root is not None:
        with tempfile.TemporaryDirectory(prefix=temp_prefix, dir=str(root)) as temporary:
            return _run(Path(temporary))
    with tempfile.TemporaryDirectory(prefix=temp_prefix) as temporary:
        return _run(Path(temporary))


def _confined_workspace(
    profile: CapabilityProfile, workspace: Workspace | None
) -> Workspace | None:
    """``workspace``, refused next to the profile's caller guard."""
    refuse_caller_guard_with_workspace(profile.guard, workspace)
    return workspace


_READ_NOTE = (
    "Your only read tool is view_file with an ABSOLUTE path under the workspace;"
    " it cannot list directories: use the file list below."
)

#: What a run's preamble tells the agent about its tools, by mode.
_TOOLS_NOTE = {
    "read": _READ_NOTE,
    "write": (
        f"{_READ_NOTE} Edit with write_to_file and replace_file_content,"
        " absolute paths under the workspace."
    ),
}


def _preamble_for(spec: RunSpec, workspace: Workspace | None) -> str:
    """The preamble ``run`` launches with, and ``build_command`` previews.

    A workspace adds the ``<files>`` block its tools note points at.
    """
    mode = "write" if workspace is not None and workspace.write else "read"
    preamble = rail_preamble(spec, tools_note=_TOOLS_NOTE[mode])
    if workspace is None:
        return preamble
    root = xml_attribute(str(workspace.path))
    files = f'<files root="{root}">\n{workspace_listing(workspace.path)}\n</files>'
    return f"{preamble}\n\n{files}"


class AgyProvider:
    """:class:`~headless_agents.protocol.AgentProvider` adapter over agy.

    Without a workspace the profile's guard is required: this package ships no
    guard for that case. With one, the package guard replaces it.
    """

    name = "agy"

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

    def build_command(self, spec: RunSpec) -> list[str]:
        workspace = _confined_workspace(spec.profile, workspace_of(spec))
        return build_agy_command(
            model=spec.model,
            prompt=prepend(_preamble_for(spec, workspace), spec.prompt),
            executable=spec.executable or "agy",
            timeout_seconds=spec.timeout_seconds,
        )

    def child_environment(self, spec: RunSpec, environ: Mapping[str, str]) -> dict[str, str] | None:
        # agy's child environment is rebuilt around the ephemeral HOME inside
        # run_agy; the bearer travels through mcp_config.json, not a variable.
        return None

    def prepare_home(self, spec: RunSpec) -> Path | None:
        environ = spec.environment if spec.environment is not None else os.environ
        return build_ephemeral_home(
            root=self._root(environ),
            name=spec.name,
            profile=spec.profile,
            real_home=self._source_home(environ),
            workspace=_confined_workspace(spec.profile, workspace_of(spec)),
        )

    def tool_call_completed(self, spec: RunSpec) -> bool:
        if spec.profile.mcp is None or spec.events_log is None:
            return False
        return tool_call_completed(spec.events_log)

    def run(self, spec: RunSpec) -> RunResult:
        workspace = _confined_workspace(spec.profile, workspace_of(spec))
        spec = spec.with_run_dir_defaults()
        assert spec.events_log is not None
        assert spec.report_log is not None
        assert spec.stderr_log is not None
        context = None if spec.context is None else tuple(spec.context.to_list())
        preamble = _preamble_for(spec, workspace)
        prompt = prepend(preamble, spec.prompt)
        # Only a prompt this rail GREW is a usage error: a caller's own prompt
        # too long for argv keeps its historical answer (3, replayable on a
        # stdin rail that can take it), through build_agy_command.
        refusal = argv_prompt_or_refusal(prompt, MAX_PROMPT_BYTES) if preamble else None
        if refusal is not None:
            spec.stderr_log.parent.mkdir(parents=True, exist_ok=True)
            spec.stderr_log.write_text(f"{refusal}\n", encoding="utf-8")
            return record(
                spec,
                RunResult(
                    exit_code=INVALID_USAGE_EXIT_CODE,
                    provider=self.name,
                    model=spec.model,
                    report_path=spec.report_log,
                    events_log=spec.events_log,
                    tokens=None,
                    duration_seconds=0.0,
                    tool_call_completed=False,
                    text=None,
                    run_id=run_id_of(spec),
                    stderr_log=spec.stderr_log,
                    workspace=workspace_summary(workspace),
                    context=context,
                ),
            )
        start = time.monotonic()
        tripwire = armed_run(workspace)
        exit_code = run_agy(
            prompt=prompt,
            name=spec.name,
            model=spec.model,
            timeout_seconds=spec.timeout_seconds,
            events_log=spec.events_log,
            report_log=spec.report_log,
            stderr_log=spec.stderr_log,
            profile=spec.profile,
            real_home=self._real_home,
            environment=spec.environment,
            executable=spec.executable or "agy",
            ephemeral_root=self._ephemeral_root,
            deadline=spec.deadline,
        )
        duration = time.monotonic() - start
        # Belt and braces: agy's own guard already denies writes under .git.
        exit_code, git_tampered = settle_run(tripwire, exit_code, spec.stderr_log)
        return record(
            spec,
            RunResult(
                exit_code=exit_code,
                provider=self.name,
                model=spec.model,
                report_path=spec.report_log,
                events_log=spec.events_log,
                tokens=None,
                duration_seconds=duration,
                tool_call_completed=(
                    spec.profile.mcp is not None and tool_call_completed(spec.events_log)
                ),
                # extract_report wrote the stream's final ``response`` there.
                text=answer_text(spec.report_log, exit_code=exit_code),
                run_id=run_id_of(spec),
                stderr_log=spec.stderr_log,
                workspace=workspace_summary(workspace, git_tampered),
                context=context,
            ),
        )
