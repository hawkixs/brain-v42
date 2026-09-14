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
    PROVIDER_FALLBACK_EXIT_CODE,
    TIMEOUT_EXIT_CODE,
    terminate_process_group,
)
from ..profile import CapabilityProfile
from ..result import RunResult
from ..sandbox import build_ephemeral_home
from ..sandbox import ephemeral_root as default_ephemeral_root
from ..spec import RunSpec

# Kernel limit on a SINGLE argument (MAX_ARG_STRLEN = 32 pages). Beyond it,
# execve returns E2BIG. We keep a margin for the rest of the command line.
MAX_PROMPT_BYTES = 120_000


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
    """
    if timeout_seconds <= 0:
        raise ValueError("timeout_seconds must be positive")
    ambient = dict(environment) if environment is not None else dict(os.environ)

    for path in (events_log, report_log, stderr_log):
        path.parent.mkdir(parents=True, exist_ok=True)
    report_log.write_text("", encoding="utf-8")

    # Fail-closed BEFORE launching anything: without a proven guard, an agy
    # run would have a free shell. Refusing to start is the only safe choice,
    # and it is logged. The path must be ABSOLUTE: the probe runs the script
    # from this process's working directory while agy resolves the hook
    # command from the ephemeral HOME, so a relative path can pass the first
    # and name nothing in the second.
    if profile.guard is None or not profile.guard.path.is_absolute():
        stderr_log.write_text(
            "agy tool guard absent or not an absolute path: run refused\n", encoding="utf-8"
        )
        return 1
    if not guard_proven and not guard_denies_machine_tools(profile.guard.path):
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
    root = ephemeral_root if ephemeral_root is not None else default_ephemeral_root(ambient)

    def _run(base: Path) -> int:
        home = build_ephemeral_home(root=base, name=name, profile=profile, real_home=source_home)
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
                    env=_child_environment(home, ambient, profile),
                    text=True,
                    start_new_session=True,
                )
            except OSError as exc:
                stderr_stream.write(f"unable to start agy: {exc}\n")
                return PROVIDER_FALLBACK_EXIT_CODE
            try:
                process.communicate(timeout=_effective_timeout(timeout_seconds, deadline))
            except subprocess.TimeoutExpired:
                terminate_process_group(process)
                return TIMEOUT_EXIT_CODE

        extract_report(events_log, report_log)
        exit_code = int(process.returncode or 0)
        if exit_code == 0:
            return 0
        if exit_code == TIMEOUT_EXIT_CODE:
            return TIMEOUT_EXIT_CODE
        if tool_call_completed(events_log):
            return exit_code
        return PROVIDER_FALLBACK_EXIT_CODE

    if root is not None:
        with tempfile.TemporaryDirectory(prefix=temp_prefix, dir=str(root)) as temporary:
            return _run(Path(temporary))
    with tempfile.TemporaryDirectory(prefix=temp_prefix) as temporary:
        return _run(Path(temporary))


class AgyProvider:
    """:class:`~headless_agents.protocol.AgentProvider` adapter over agy.

    The profile's guard is required: this package ships no guard of its own.
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
        return build_agy_command(
            model=spec.model,
            prompt=spec.prompt,
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
        )

    def tool_call_completed(self, spec: RunSpec) -> bool:
        if spec.profile.mcp is None or spec.events_log is None:
            return False
        return tool_call_completed(spec.events_log)

    def run(self, spec: RunSpec) -> RunResult:
        assert spec.events_log is not None
        assert spec.report_log is not None
        assert spec.stderr_log is not None
        start = time.monotonic()
        exit_code = run_agy(
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
            executable=spec.executable or "agy",
            ephemeral_root=self._ephemeral_root,
            deadline=spec.deadline,
        )
        duration = time.monotonic() - start
        return RunResult(
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
        )
