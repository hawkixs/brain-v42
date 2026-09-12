"""Byte-identical log-line formatters for the Dream phase runner.

Every template here is quoted verbatim from ``scripts/dream.sh`` at
``c0e48966`` (see ``tests/fixtures/agents_chain_golden/log_lines.json``,
captured before ``run_phase``/``run_phase_chain`` moved to Python). Lot 2 of
the agent runtime extraction, Brain ticket ``afd56820``.

These functions produce the raw line only -- no ``[HH:MM:SS]`` prefix, no
printing, no file append. ``prefixed()`` adds the prefix; the caller (
:func:`brain_v42.agents.phase.make_logger`) is the one that also prints and
appends to the main log, mirroring bash's ``log() { echo ... | tee -a ...; }``.
"""

from __future__ import annotations

from datetime import time
from pathlib import Path

# Bash `dream_wants_wet`/parser labels: which per-rail metrics parser produced
# the WARN line for a given provider. Note the claude rail's module is named
# `dream_parser`, not `claude_dream_parser`.
_PARSER_WARN_LABEL = {
    "agy": "agy_dream_parser",
    "codex": "codex_dream_parser",
    "claude": "dream_parser",
}


def start_line(
    provider: str,
    name: str,
    model: str,
    timeout_minutes: int,
    *,
    reasoning: str | None = None,
    max_turns: int | None = None,
) -> str:
    if provider == "agy":
        return f"START {name} (provider=agy, model={model}, timeout={timeout_minutes}m)"
    if provider == "codex":
        return (
            f"START {name} (provider=codex, model={model}, "
            f"reasoning={reasoning}, timeout={timeout_minutes}m)"
        )
    return (
        f"START {name} (provider=claude, model={model}, "
        f"timeout={timeout_minutes}m, max_turns={max_turns})"
    )


def done_line(name: str) -> str:
    return f"DONE  {name}"


def timeout_line(name: str, timeout_minutes: int) -> str:
    return f"TIMEOUT {name} (>{timeout_minutes}m)"


def fail_line(name: str, code: int, *, fallback: bool) -> str:
    if fallback:
        return f"FAIL  {name} (exit={code} — aucun appel d'outil Brain abouti)"
    return f"FAIL  {name} (exit={code})"


def skip_missing_prompt_line(name: str, prompt_file: Path) -> str:
    return f"SKIP {name} — prompt file missing: {prompt_file}"


def unsupported_tier_line(name: str, provider: str, model_tier: str) -> str:
    return f"FAIL  {name} — unsupported provider/model tier: {provider}/{model_tier}"


def otel_warn_line(name: str) -> str:
    return f"WARN  otel_split failed for {name} — leaving raw log in place"


def parser_warn_line(provider: str, name: str) -> str:
    return f"WARN  {_PARSER_WARN_LABEL[provider]} failed for {name} (non-fatal)"


def killswitch_line(var_name: str, value: str) -> str:
    return f"KILLSWITCH {var_name}='{value}' is neither 'true' nor 'false' — staying DRY"


def fallback_line(project_key: str, name: str, provider: str, next_provider: str) -> str:
    return (
        f"FALLBACK {project_key}/{name} — {provider} a échoué sans aucun appel "
        f"d'outil Brain abouti, bascule vers {next_provider}"
    )


def fallback_end_line(project_key: str, name: str, provider: str) -> str:
    return f"FALLBACK-END {project_key}/{name} — {provider} était le dernier maillon de la chaîne"


def prefixed(line: str, now: time) -> str:
    return f"[{now.strftime('%H:%M:%S')}] {line}"
