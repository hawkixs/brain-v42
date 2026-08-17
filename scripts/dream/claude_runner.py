"""Isolated ``claude -p`` adapter for one Dream phase.

The Claude rail predates the capability firewall.  It ran through the
repository's static ``.mcp.json``, whose ``Authorization`` header expands
``${MCP_HTTP_TOKEN}`` — the ADMIN token — and through the
``mcp__brain-v42__*`` wildcard.  Under enforcement that combination is exactly
what the firewall exists to forbid, so ``dream.sh`` refused the provider
outright instead.

This module makes the refusal unnecessary.  It renders a per-phase MCP client
configuration (scoped agent header, no wildcard) and hands the child process
the ``(project, phase)`` bearer under the name the configuration references.
The secret is therefore never written to disk and never appears in ``argv`` —
the same trade ``codex_runner`` makes.

The five ``dream.sh`` exports that belong to this rail travel through
``_CLAUDE_CHILD_ENV_EXTRA``.  Dropping them would not fail the phase; it would
make it succeed blind, which is worse.
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
    MCP_TOKEN_ENV,
    MCP_URL_ENV,
    PROVIDER_FALLBACK_EXIT_CODE,
    TIMEOUT_EXIT_CODE,
    build_child_environment,
    preflight_capabilities,
    terminate_process_group,
)

PHASE_TOOL_ALLOWLISTS = DREAM_PHASE_TOOL_ALLOWLISTS

# Exported by dream.sh for this rail only.
#
# The three OTEL variables are what otel_split consumes; without them a phase
# still exits 0 but its metrics row loses every token count.
#
# MCP_CONNECTION_NONBLOCKING and MCP_CONNECT_TIMEOUT_MS are the fix for
# regression 27430ae1: `claude -p` otherwise snapshots the turn's tool list
# ~450ms after init, while the brain-v42 server needs longer to register its
# tools. The phase then runs with NO brain tools and reports success — the
# false-green failure this project has now paid for twice.
_CLAUDE_CHILD_ENV_EXTRA = frozenset(
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


def claude_child_environment(
    *,
    project_key: str | None,
    phase: str,
    environ: Mapping[str, str],
) -> dict[str, str] | None:
    """Return the phase-scoped child environment, or ``None`` if unenforced."""
    return build_child_environment(
        project_key=project_key,
        phase=phase,
        environ=environ,
        extra_allowlist=_CLAUDE_CHILD_ENV_EXTRA,
    )


def build_claude_mcp_config(*, phase: str, mcp_url: str) -> dict[str, object]:
    """Render the per-phase MCP client configuration for ``claude -p``.

    ``dream_phase_tool_allowlist`` raises on an unknown phase, which keeps an
    unrecognised phase from silently producing a config with no restriction.
    """
    dream_phase_tool_allowlist(phase)
    return {
        "mcpServers": {
            "brain-v42": {
                "type": "http",
                "url": mcp_url,
                "headers": {
                    "X-Brain-Agent": f"dream-claude-{phase}",
                    "X-Brain-Tool-Profile": "native",
                    # Expanded by the client from the child environment, where
                    # the scoped bearer has replaced the admin token.
                    "Authorization": f"Bearer ${{{MCP_TOKEN_ENV}}}",
                },
            }
        }
    }


def build_claude_command(
    *,
    phase: str,
    model: str,
    max_turns: int,
    mcp_config_path: Path,
    claude_executable: str = "claude",
) -> list[str]:
    """Build the hardened non-interactive Claude command for one phase."""
    if not model.strip():
        raise ValueError("Claude model must not be empty")
    if max_turns <= 0:
        raise ValueError("max_turns must be positive")

    # The exact per-phase allowlist replaces the historical
    # `mcp__brain-v42__*` wildcard: the wildcard is what the capability
    # firewall exists to remove, and leaving it here would make the scoped
    # bearer the only line of defence.
    allowed_tools = ",".join(
        f"mcp__brain-v42__{tool}" for tool in dream_phase_tool_allowlist(phase)
    )
    return [
        claude_executable,
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
        "--allowedTools",
        allowed_tools,
        "--mcp-config",
        str(mcp_config_path),
        "--strict-mcp-config",
    ]


def brain_tool_call_completed(raw_log: Path) -> bool:
    """Un appel d'outil Brain a-t-il ABOUTI dans cette télémétrie OTEL ?

    Pendant du prédicat de ``codex_runner``, sur la seule source que le rail
    claude expose : le flux OTEL console mélangé au rapport dans ``raw_log``.

    Forme MESURÉE le 2026-08-11 sur claude 2.1.226, pas déduite d'une doc :

        body: "claude_code.tool_result"
        attributes: { tool_name: "mcp_tool", success: "true", ... }

    ``tool_name`` vaut génériquement ``mcp_tool`` — il ne nomme PAS l'outil.
    Ce n'est pas une lacune ici : le runner ne déclare que brain-v42 et pose
    ``--strict-mcp-config``, donc tout résultat MCP abouti est nécessairement
    un appel Brain. Si un jour un second serveur MCP est déclaré sur ce rail,
    ce raisonnement tombe et ce prédicat doit être resserré.
    """
    if not raw_log.is_file():
        return False
    content = raw_log.read_text(encoding="utf-8", errors="replace")
    # Le flux console est du pseudo-JSON multi-lignes, pas du JSON : on
    # découpe sur l'enregistrement plutôt que de le parser.
    for record in content.split('body: "claude_code.tool_result"')[1:]:
        window = record[:2000]
        if 'tool_name: "mcp_tool"' in window and 'success: "true"' in window:
            return True
    return False


def run_claude(
    *,
    prompt: str,
    phase: str,
    project_key: str | None = None,
    model: str,
    max_turns: int,
    timeout_seconds: float,
    raw_log: Path,
    claude_executable: str = "claude",
) -> int:
    """Run one Claude phase and return its exit code (``124`` on timeout).

    The mixed stdout/stderr stream keeps landing in ``raw_log`` because
    ``dream.sh`` still feeds it to ``otel_split``; separating the streams here
    would silently deprive the metrics rail of its telemetry.
    """
    if timeout_seconds <= 0:
        raise ValueError("timeout_seconds must be positive")
    try:
        child_environment = claude_child_environment(
            project_key=project_key,
            phase=phase,
            environ=os.environ,
        )
    except DreamCapabilityConfigurationError:
        raw_log.parent.mkdir(parents=True, exist_ok=True)
        with raw_log.open("a", encoding="utf-8") as stream:
            stream.write(f"{CAPABILITY_CONFIGURATION_ERROR}\n")
        return 1

    if child_environment is None and not os.environ.get(MCP_TOKEN_ENV):
        raw_log.parent.mkdir(parents=True, exist_ok=True)
        with raw_log.open("a", encoding="utf-8") as stream:
            stream.write(f"missing required environment variable: {MCP_TOKEN_ENV}\n")
        return 1

    raw_log = raw_log.resolve()
    raw_log.parent.mkdir(parents=True, exist_ok=True)
    mcp_url = os.environ.get(MCP_URL_ENV, DEFAULT_MCP_URL)

    with tempfile.TemporaryDirectory(prefix=f"brain-v42-dream-claude-{phase}-") as temp_dir:
        runtime_dir = Path(temp_dir)
        mcp_config_path = runtime_dir / "mcp-config.json"
        mcp_config_path.write_text(
            json.dumps(build_claude_mcp_config(phase=phase, mcp_url=mcp_url)),
            encoding="utf-8",
        )
        command = build_claude_command(
            phase=phase,
            model=model,
            max_turns=max_turns,
            mcp_config_path=mcp_config_path,
            claude_executable=claude_executable,
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
                # Claude n'a pas démarré : rien n'a pu être écrit.
                return PROVIDER_FALLBACK_EXIT_CODE

            try:
                process.communicate(input=prompt, timeout=timeout_seconds)
            except subprocess.TimeoutExpired:
                terminate_process_group(process)
                # Un timeout ne prouve RIEN : la phase a pu écrire puis rester
                # bloquée. Jamais de bascule ici.
                return 124

    exit_code = int(process.returncode or 0)
    if exit_code == 0:
        return 0
    if exit_code == TIMEOUT_EXIT_CODE:
        return TIMEOUT_EXIT_CODE
    # Le rail claude ne peut pas prouver l'absence d'écriture autrement que par
    # sa télémétrie : sans les OTEL_* exportés par dream.sh, raw_log ne contient
    # aucun événement d'outil et le prédicat renverra False. C'est le bon défaut
    # — il autorise la bascule sur un rail qui n'a manifestement rien fait — et
    # c'est aussi pourquoi ces variables sont dans _CLAUDE_CHILD_ENV_EXTRA.
    if brain_tool_call_completed(raw_log):
        return exit_code
    return PROVIDER_FALLBACK_EXIT_CODE


def _build_arg_parser() -> argparse.ArgumentParser:
    parser = argparse.ArgumentParser(description="Run one isolated Dream phase with Claude")
    parser.add_argument("--preflight-capabilities", action="store_true")
    parser.add_argument("--project-key")
    parser.add_argument("--phase", choices=tuple(PHASE_TOOL_ALLOWLISTS))
    parser.add_argument("--model")
    parser.add_argument("--max-turns", type=int)
    parser.add_argument("--timeout-seconds", type=float)
    parser.add_argument("--raw-log", type=Path)
    parser.add_argument(
        "--claude-executable", default=os.environ.get("BRAIN_DREAM_CLAUDE_BIN", "claude")
    )
    return parser


def main(argv: Sequence[str] | None = None) -> int:
    parser = _build_arg_parser()
    args = parser.parse_args(argv)
    if args.preflight_capabilities:
        if args.project_key is None:
            parser.error("--project-key is required with --preflight-capabilities")
        try:
            preflight_capabilities(args.project_key, os.environ)
        except DreamCapabilityConfigurationError:
            print(CAPABILITY_CONFIGURATION_ERROR, file=sys.stderr)
            return 1
        return 0

    required_arguments = {
        "--phase": args.phase,
        "--model": args.model,
        "--max-turns": args.max_turns,
        "--timeout-seconds": args.timeout_seconds,
        "--raw-log": args.raw_log,
    }
    missing = [name for name, value in required_arguments.items() if value is None]
    if missing:
        parser.error(f"the following arguments are required: {', '.join(missing)}")

    prompt = sys.stdin.read()
    if not prompt.strip():
        print("Dream Claude prompt is empty", file=sys.stderr)
        return 1
    return run_claude(
        prompt=prompt,
        phase=args.phase,
        project_key=args.project_key,
        model=args.model,
        max_turns=args.max_turns,
        timeout_seconds=args.timeout_seconds,
        raw_log=args.raw_log,
        claude_executable=args.claude_executable,
    )


if __name__ == "__main__":
    raise SystemExit(main())
