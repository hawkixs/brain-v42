"""An isolated ``agy`` adapter for one Dream phase.

agy takes NO configuration on the command line: no ``--mcp-config``, no tool
allowlist, no equivalent of claude's ``--tools ""``. Its bundled documentation
(``docs/mcp_servers.md``) knows only two locations, both global, and a
project-level ``.agents/hooks.json`` is NOT discovered — measured 2026-08-11,
in a trusted workspace and a git repository.

Hence the ephemeral HOME: it is the only route that gives per-invocation
control. agy finds ``.gemini/config/{mcp_config.json,hooks.json}`` there and
authenticates through credentials symlinked from the real HOME.

What it avoids counts as much as what it allows. Without it, the rail's security
would rest on a global file outside the repository, which a manual edit or an
agy upgrade would remove in silence — and two concurrent phases would tread on
each other by rewriting the same ``mcp_config.json``.

TWO PROTECTIONS, TWO PERIMETERS, never to be confused:
- ``agy_tool_guard.sh``, wired as a ``PreToolUse`` hook, protects the MACHINE;
- the ``(project, phase)`` bearer protects the CORPUS, and the server is what
  enforces it.

THE RAIL'S ONLY DEVIATION. agy's ``Authorization`` is a literal: its
documentation describes no ``${VAR}`` interpolation, unlike the ``.mcp.json``
claude reads. The bearer is therefore WRITTEN to a file where the other two
rails pass it through the environment. It is confined to a 0700 HOME under
``XDG_RUNTIME_DIR`` — a tmpfs, never persistent disk — and destroyed with it.
Named here so it is not rediscovered by accident.
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
    CAPABILITY_CONFIGURATION_ERROR,
    DEFAULT_MCP_URL,
    MCP_URL_ENV,
    PROVIDER_FALLBACK_EXIT_CODE,
    TIMEOUT_EXIT_CODE,
    active_capability_token,
    capability_enforcement_enabled,
    preflight_capabilities,
    terminate_process_group,
    validate_loopback_mcp_url,
)

PHASE_TOOL_ALLOWLISTS = DREAM_PHASE_TOOL_ALLOWLISTS

GUARD_PATH = Path(__file__).resolve().parent / "agy_tool_guard.sh"

# Identity files read from the real HOME. SYMLINKED, never copied: duplicating
# a human's OAuth tokens would make copies to revoke one by one.
_CREDENTIAL_PATHS = (
    ".gemini/oauth_creds.json",
    ".gemini/google_accounts.json",
    ".gemini/gemini-credentials.json",
    ".gemini/antigravity-cli/antigravity-oauth-token",
)


def ephemeral_root(environ: Mapping[str, str]) -> Path | None:
    """Root of the ephemeral HOMEs — a tmpfs by preference.

    The bearer is written there: it must not land on persistent disk. ``None``
    means "no tmpfs available", and lets the caller fall back on ``tempfile``
    rather than inventing a path.
    """
    runtime_dir = environ.get("XDG_RUNTIME_DIR")
    if runtime_dir and Path(runtime_dir).is_dir():
        return Path(runtime_dir)
    return None


def build_ephemeral_home(
    *,
    root: Path,
    phase: str,
    project_key: str,
    environ: Mapping[str, str],
    real_home: Path,
    mcp_url: str | None = None,
) -> Path:
    """Compose a phase HOME: scoped bearer, wired guard, nothing else."""
    dream_phase_tool_allowlist(phase)
    validate_loopback_mcp_url(environ)
    token = active_capability_token(project_key=project_key, phase=phase, environ=environ)

    home = root / f"agy-{project_key.replace(':', '-')}-{phase}"
    config_dir = home / ".gemini" / "config"
    config_dir.mkdir(parents=True, exist_ok=True)
    (home / ".gemini" / "antigravity-cli").mkdir(parents=True, exist_ok=True)
    home.chmod(0o700)

    # The real HOME's mcp_config also declares red-writer, on a public URL.
    # We do not copy it: we write one that knows brain-v42 and nothing else.
    server_url = mcp_url or environ.get(MCP_URL_ENV, DEFAULT_MCP_URL)
    mcp_config = {
        "mcpServers": {
            "brain-v42": {
                "serverUrl": server_url,
                "headers": {
                    "Authorization": f"Bearer {token}",
                    "X-Brain-Agent": f"dream-agy-{phase}",
                    "X-Brain-Tool-Profile": "native",
                },
                "trust": True,
            }
        }
    }
    config_path = config_dir / "mcp_config.json"
    config_path.write_text(json.dumps(mcp_config), encoding="utf-8")
    config_path.chmod(0o600)

    # The guard comes from the REPOSITORY. A copy dropped next to the secret
    # would be editable without review and would drift from its tests.
    hooks = {
        "dream-phase-guard": {
            "PreToolUse": [
                {
                    "matcher": "*",
                    "hooks": [{"type": "command", "command": str(GUARD_PATH), "timeout": 10}],
                }
            ]
        }
    }
    (config_dir / "hooks.json").write_text(json.dumps(hooks), encoding="utf-8")

    # The trusted workspace must be the ephemeral HOME itself: without it, agy
    # refuses to load its customisations.
    (home / ".gemini" / "antigravity-cli" / "settings.json").write_text(
        json.dumps({"enableTelemetry": False, "trustedWorkspaces": [str(home)]}),
        encoding="utf-8",
    )

    for relative in _CREDENTIAL_PATHS:
        source = real_home / relative
        target = home / relative
        target.parent.mkdir(parents=True, exist_ok=True)
        if source.exists() and not target.exists():
            target.symlink_to(source)

    return home


# Kernel limit on a SINGLE argument (MAX_ARG_STRLEN = 32 pages). Beyond it,
# execve returns E2BIG. We keep a margin for the rest of the command line.
_MAX_PROMPT_BYTES = 120_000


def build_agy_command(
    *,
    model: str,
    prompt: str,
    agy_executable: str = "agy",
    timeout_seconds: float = 300.0,
) -> list[str]:
    """The headless command line of one phase.

    THE PROMPT GOES IN ARGV, and that is not a choice. Measured 2026-08-11: agy
    IGNORES stdin — ``--print ""`` with the prompt on stdin returns an empty
    answer, and a prompt in an argument plus context on stdin answers without
    the context. The other two rails deliberately go through stdin to dodge
    ARG_MAX; agy leaves no such option.

    The failure mode if you get it wrong is treacherous: agy answers all the
    same, with a greeting, and the phase exits 0 with an off-topic report.

    No secret travels through argv: the bearer lives in the ephemeral HOME's
    ``mcp_config.json``. The prompt is visible there — it is phase instructions
    and previous reports, not a secret.

    ``--dangerously-skip-permissions`` is REQUIRED: without it, agy waits in
    headless mode for an approval that will never come. It is not what bounds
    the phase — that is the ``PreToolUse`` guard, which survives this flag.
    """
    prompt_bytes = len(prompt.encode("utf-8"))
    if prompt_bytes > _MAX_PROMPT_BYTES:
        # Refuse BEFORE execve: an E2BIG deep inside a Popen is an opaque
        # OSError, where this names the cause and its size.
        raise ValueError(
            f"prompt trop long pour argv : {prompt_bytes} octets > {_MAX_PROMPT_BYTES}"
        )
    command = [
        agy_executable,
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


def brain_tool_call_completed(events_log: Path) -> bool:
    """Did a Brain tool call SUCCEED in this stream-json flow?

    The counterpart of codex's and claude's predicates. ``False`` proves no
    mutation was committed, hence that replaying the phase elsewhere carries no
    risk.

    Only ``call_mcp_tool`` counts: it is the gateway through which agy reaches
    brain-v42, and the guard refuses everything else. A REFUSED ``run_command``
    does produce a tool step — counting it would block the switchover on a
    phase that plainly wrote nothing.
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


def guard_denies_machine_tools(guard: Path | None = None) -> bool:
    """PROVE the guard refuses, instead of noting that it exists.

    The guard is the only wall between a nightly phase and a shell. Checking it
    is present would let through a guard that is empty, non-executable,
    misnamed or made permissive by an edit — all states in which the file
    exists. So we submit a real payload to it and demand the refusal.
    """
    guard_path = guard or GUARD_PATH
    if not guard_path.is_file():
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
                ["bash", str(guard_path)],
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


def run_agy(
    *,
    prompt: str,
    phase: str,
    project_key: str,
    model: str,
    timeout_seconds: float,
    events_log: Path,
    report_log: Path,
    stderr_log: Path,
    agy_executable: str = "agy",
) -> int:
    """Run a phase and return its code (``124`` on deadline, ``3`` if replayable)."""
    if timeout_seconds <= 0:
        raise ValueError("timeout_seconds must be positive")

    for path in (events_log, report_log, stderr_log):
        path.parent.mkdir(parents=True, exist_ok=True)
    report_log.write_text("", encoding="utf-8")

    # Fail-closed BEFORE launching anything: without a proven guard, an agy
    # phase would have a free shell. Refusing to start is the only safe
    # choice, and it is logged.
    if not guard_denies_machine_tools():
        stderr_log.write_text(
            "garde d'outils agy absente ou permissive — phase refusée\n", encoding="utf-8"
        )
        return 1

    if not capability_enforcement_enabled(os.environ):
        stderr_log.write_text(
            "le rail agy exige BRAIN_DREAM_CAPABILITY_ENFORCEMENT=true\n", encoding="utf-8"
        )
        return 1

    real_home = Path(os.environ.get("HOME", str(Path.home())))
    root = ephemeral_root(os.environ)

    def _run(base: Path) -> int:
        try:
            home = build_ephemeral_home(
                root=base,
                phase=phase,
                project_key=project_key,
                environ=os.environ,
                real_home=real_home,
            )
        except DreamCapabilityConfigurationError:
            stderr_log.write_text(f"{CAPABILITY_CONFIGURATION_ERROR}\n", encoding="utf-8")
            return 1

        child_environment = {
            "HOME": str(home),
            "PATH": os.environ.get("PATH", "/usr/bin:/bin"),
            "LANG": os.environ.get("LANG", "C.UTF-8"),
            "TERM": "dumb",
        }
        try:
            command = build_agy_command(
                model=model,
                prompt=prompt,
                agy_executable=agy_executable,
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
                    env=child_environment,
                    text=True,
                    start_new_session=True,
                )
            except OSError as exc:
                stderr_stream.write(f"impossible de démarrer agy : {exc}\n")
                return PROVIDER_FALLBACK_EXIT_CODE
            try:
                process.communicate(timeout=timeout_seconds)
            except subprocess.TimeoutExpired:
                terminate_process_group(process)
                return TIMEOUT_EXIT_CODE

        extract_report(events_log, report_log)
        exit_code = int(process.returncode or 0)
        if exit_code == 0:
            return 0
        if exit_code == TIMEOUT_EXIT_CODE:
            return TIMEOUT_EXIT_CODE
        if brain_tool_call_completed(events_log):
            return exit_code
        return PROVIDER_FALLBACK_EXIT_CODE

    if root is not None:
        with tempfile.TemporaryDirectory(prefix="brain-v42-dream-", dir=str(root)) as temporary:
            return _run(Path(temporary))
    with tempfile.TemporaryDirectory(prefix="brain-v42-dream-") as temporary:
        return _run(Path(temporary))


def extract_report(events_log: Path, report_log: Path) -> None:
    """Rebuild the phase report from the event stream.

    The final answer lives under ``{"event":"result","result":{"response":...}}``.
    Looking for it elsewhere produces an EMPTY report, and dream.sh injects this
    file into the next phase then hands it to its validators: the dependency
    chain would break without an error, the phase exiting 0.
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


def _build_arg_parser() -> argparse.ArgumentParser:
    parser = argparse.ArgumentParser(description="Jouer une phase de Dream avec agy")
    parser.add_argument("--preflight-capabilities", action="store_true")
    parser.add_argument("--project-key")
    parser.add_argument("--phase", choices=tuple(PHASE_TOOL_ALLOWLISTS))
    parser.add_argument("--model", default="")
    parser.add_argument("--timeout-seconds", type=float)
    parser.add_argument("--events-log", type=Path)
    parser.add_argument("--report-log", type=Path)
    parser.add_argument("--stderr-log", type=Path)
    parser.add_argument("--agy-executable", default=os.environ.get("BRAIN_DREAM_AGY_BIN", "agy"))
    return parser


def main(argv: Sequence[str] | None = None) -> int:
    parser = _build_arg_parser()
    args = parser.parse_args(argv)

    if args.preflight_capabilities:
        if args.project_key is None:
            parser.error("--project-key est requis avec --preflight-capabilities")
        if not guard_denies_machine_tools():
            print("garde d'outils agy absente ou permissive", file=sys.stderr)
            return 1
        try:
            preflight_capabilities(args.project_key, os.environ)
        except DreamCapabilityConfigurationError:
            print(CAPABILITY_CONFIGURATION_ERROR, file=sys.stderr)
            return 1
        return 0

    required = {
        "--phase": args.phase,
        "--project-key": args.project_key,
        "--timeout-seconds": args.timeout_seconds,
        "--events-log": args.events_log,
        "--report-log": args.report_log,
        "--stderr-log": args.stderr_log,
    }
    missing = [name for name, value in required.items() if value is None]
    if missing:
        parser.error(f"arguments requis manquants : {', '.join(missing)}")

    prompt = sys.stdin.read()
    if not prompt.strip():
        print("prompt de phase agy vide", file=sys.stderr)
        return 1
    return run_agy(
        prompt=prompt,
        phase=args.phase,
        project_key=args.project_key,
        model=args.model,
        timeout_seconds=args.timeout_seconds,
        events_log=args.events_log,
        report_log=args.report_log,
        stderr_log=args.stderr_log,
        agy_executable=args.agy_executable,
    )


if __name__ == "__main__":
    raise SystemExit(main())
