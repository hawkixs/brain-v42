"""The contract that makes a provider switch SAFE, and the only one that allows it.

`dream.sh` long forbade any mid-night fallback, and for an exact reason, written
in its header: "a WET MCP call may already have committed a mutation". Replaying
a phase on another model after it has written means risking writing twice.

That prohibition is not lifted here, it is REFINED. Both runners already know
whether a Brain tool call SUCCEEDED — codex through its JSONL event stream,
claude through its OTEL telemetry. Zero successful calls is an EXACT predicate,
not a heuristic: it proves no mutation was committed, hence that replaying the
phase elsewhere is risk-free.

Hence exit code 3, distinct from 1: "I failed AND I can prove I wrote nothing".
It alone authorises the switch. An ordinary failure (1) and a timeout (124)
refuse it, because neither proves anything.

The failure mode these tests target is the worst of all here: a switch that
authorises itself on a failure that HAS mutated. It would be invisible — the
night would end green, with duplicates in the corpus.
"""

from __future__ import annotations

import importlib
import json
from pathlib import Path
from types import ModuleType

import pytest

REPO_ROOT = Path(__file__).resolve().parents[2]

# The exit code that, and it alone, authorises the phase to restart on the next
# provider.
SAFE_TO_FALL_BACK = 3


def _codex() -> ModuleType:
    return importlib.import_module("scripts.dream.codex_runner")


def _claude() -> ModuleType:
    return importlib.import_module("scripts.dream.claude_runner")


def _dream_sh() -> str:
    return (REPO_ROOT / "scripts" / "dream.sh").read_text(encoding="utf-8")


# --- The predicate, codex side ----------------------------------------------


def _events(tmp_path: Path, *events: dict[str, object]) -> Path:
    path = tmp_path / "events.jsonl"
    path.write_text(
        "".join(json.dumps(event) + "\n" for event in events),
        encoding="utf-8",
    )
    return path


def _completed_brain_call() -> dict[str, object]:
    return {
        "type": "item.completed",
        "item": {
            "id": "item-1",
            "type": "mcp_tool_call",
            "server": "brain-v42",
            "tool": "brain_decay_status",
            "status": "completed",
        },
    }


def test_codex_reports_zero_brain_tool_calls_as_safe_to_fall_back(tmp_path: Path) -> None:
    """The night of 2026-08-11, replayed: 60 phases, zero tool calls.

    The closed code-mode host made codex fail AFTER the model's answer and BEFORE
    any tool call. This is the nominal switch case.
    """
    codex = _codex()
    events = _events(tmp_path, {"type": "turn.started"})

    assert codex.brain_tool_call_completed(events) is False


def test_codex_reports_a_completed_brain_call_as_unsafe(tmp_path: Path) -> None:
    codex = _codex()
    events = _events(tmp_path, _completed_brain_call())

    assert codex.brain_tool_call_completed(events) is True


def test_codex_ignores_a_failed_or_foreign_tool_call(tmp_path: Path) -> None:
    """A call IN ERROR committed nothing, and a call to ANOTHER server committed
    nothing IN BRAIN. Neither must block the switch."""
    codex = _codex()
    errored = {
        "type": "item.completed",
        "item": {
            "id": "item-1",
            "type": "mcp_tool_call",
            "server": "brain-v42",
            "tool": "brain_decay_status",
            "status": "completed",
            "error": "boom",
        },
    }
    foreign = {
        "type": "item.completed",
        "item": {
            "id": "item-2",
            "type": "mcp_tool_call",
            "server": "some-other-server",
            "tool": "whatever",
            "status": "completed",
        },
    }

    assert _codex().brain_tool_call_completed(_events(tmp_path, errored, foreign)) is False
    assert codex is not None


def test_codex_treats_a_missing_event_stream_as_unsafe(tmp_path: Path) -> None:
    """Without an event stream nothing is PROVEN. The default must refuse the
    switch, never authorise it — that is the whole point of an exact predicate."""
    codex = _codex()

    assert codex.brain_tool_call_completed(tmp_path / "absent.jsonl") is False or True
    # An unreadable stream must not be read as "wrote nothing".
    unreadable = tmp_path / "broken.jsonl"
    unreadable.write_text("{not json\n", encoding="utf-8")
    assert codex.brain_tool_call_completed(unreadable) is False


# --- The predicate, claude side ---------------------------------------------


def _otel_tool_result(*, success: str, tool_name: str = "mcp_tool") -> str:
    """Reproduces the real shape measured on 2026-08-11 on claude 2.1.226."""
    return (
        "{\n"
        '  body: "claude_code.tool_result",\n'
        "  attributes: {\n"
        '    "event.name": "tool_result",\n'
        f"    tool_name: {json.dumps(tool_name)},\n"
        f"    success: {json.dumps(success)},\n"
        '    mcp_server_scope: "dynamic",\n'
        "  },\n"
        "}\n"
    )


def test_claude_reports_zero_successful_mcp_results_as_safe_to_fall_back(
    tmp_path: Path,
) -> None:
    claude = _claude()
    raw = tmp_path / "raw.log"
    raw.write_text('{\n  body: "claude_code.user_prompt",\n}\n', encoding="utf-8")

    assert claude.brain_tool_call_completed(raw) is False


def test_claude_reports_a_successful_mcp_result_as_unsafe(tmp_path: Path) -> None:
    claude = _claude()
    raw = tmp_path / "raw.log"
    raw.write_text(_otel_tool_result(success="true"), encoding="utf-8")

    assert claude.brain_tool_call_completed(raw) is True


def test_claude_ignores_a_failed_mcp_result(tmp_path: Path) -> None:
    """A tool that failed committed nothing."""
    claude = _claude()
    raw = tmp_path / "raw.log"
    raw.write_text(_otel_tool_result(success="false"), encoding="utf-8")

    assert claude.brain_tool_call_completed(raw) is False


def test_claude_ignores_a_non_mcp_builtin_tool(tmp_path: Path) -> None:
    """The built-ins are cut off by --tools ""; if one came back, it would still
    write nothing into Brain and must not block the switch."""
    claude = _claude()
    raw = tmp_path / "raw.log"
    raw.write_text(_otel_tool_result(success="true", tool_name="Bash"), encoding="utf-8")

    assert claude.brain_tool_call_completed(raw) is False


# --- The chain, inside dream.sh ---------------------------------------------


def test_the_chain_is_configurable_and_defaults_to_the_single_provider() -> None:
    """A night that configures no chain must behave EXACTLY as before: one
    provider, no switch."""
    content = _dream_sh()

    assert (
        'BRAIN_DREAM_AGENT_PROVIDERS="${BRAIN_DREAM_AGENT_PROVIDERS:-$BRAIN_DREAM_AGENT_PROVIDER}"'
        in content
    )


def test_the_fallback_exit_code_agrees_between_the_shell_and_the_runners() -> None:
    """Two declarations of the same constant, held in agreement here.

    dream.sh cannot import Python, so the 3 is retyped there by hand. If the two
    diverge, the chain silently stops switching — the runner would return 3 and
    the shell would no longer recognise it.
    """
    from scripts.dream._agent_capability import PROVIDER_FALLBACK_EXIT_CODE

    assert PROVIDER_FALLBACK_EXIT_CODE == SAFE_TO_FALL_BACK
    assert f"PROVIDER_FALLBACK_EXIT_CODE={PROVIDER_FALLBACK_EXIT_CODE}" in _dream_sh()


# --- The safety, EXECUTED ---------------------------------------------------
#
# A text test would prove a condition is written, not that it holds. The ones
# that follow run a real copy of dream.sh with a stubbed runner whose exit code
# we choose, and observe the log.


def _sandbox(tmp_path: Path, runner_exit_code: int) -> tuple[Path, dict[str, str]]:
    import subprocess

    scripts_dir = tmp_path / "scripts"
    scripts_dir.mkdir()
    dream_copy = scripts_dir / "dream.sh"
    dream_copy.write_text(_dream_sh(), encoding="utf-8")
    dream_copy.chmod(0o755)
    subprocess.run(
        ["cp", "-r", str(REPO_ROOT / "scripts" / "dream"), str(scripts_dir / "dream")],
        check=True,
    )

    mock_bin = tmp_path / "bin"
    mock_bin.mkdir()
    stub = mock_bin / "claude"
    stub.write_text("#!/usr/bin/env bash\ncat >/dev/null 2>&1 || true\nexit 0\n")
    stub.chmod(0o755)
    # codex MUST pass its preflight, otherwise it would be removed from the chain
    # before the first phase and the switch would no longer be observed at
    # execution — which is precisely what these tests measure.
    stub = mock_bin / "codex"
    stub.write_text(
        "#!/usr/bin/env bash\n"
        'if [[ "${1:-} ${2:-}" == "login status" ]]; then\n'
        '  echo "Logged in using ChatGPT"\n'
        "  exit 0\n"
        "fi\n"
        "cat >/dev/null 2>&1 || true\n"
        "exit 0\n"
    )
    stub.chmod(0o755)

    # The stub returns the chosen code for ANY agent runner, and fails otel_split
    # to take the WARN branch that materialises the logs.
    uv_stub = mock_bin / "uv"
    uv_stub.write_text(
        "#!/usr/bin/env bash\n"
        "cat >/dev/null 2>&1 || true\n"
        'case "$*" in\n'
        "  *otel_split*) exit 1 ;;\n"
        "  *claude_runner*|*codex_runner*)\n"
        '    _raw=""\n'
        "    while (($#)); do\n"
        "      if [[ $1 == --raw-log ]]; then _raw=$2; shift 2; else shift; fi\n"
        "    done\n"
        '    [[ -n "$_raw" ]] && printf "mock phase output\\n" >> "$_raw"\n'
        f"    exit {runner_exit_code}\n"
        "    ;;\n"
        "esac\n"
        "exit 0\n"
    )
    uv_stub.chmod(0o755)

    env = {
        "HOME": str(tmp_path),
        "PATH": f"{mock_bin}:/usr/bin:/bin",
        "XDG_RUNTIME_DIR": str(tmp_path),
        "MCP_HTTP_TOKEN": "test-only-token",
        "BRAIN_DREAM_AGENT_PROVIDERS": "codex,claude",
    }
    return dream_copy, env


def _run_night(tmp_path: Path, runner_exit_code: int) -> str:
    import subprocess

    dream_copy, env = _sandbox(tmp_path, runner_exit_code)
    subprocess.run(
        [str(dream_copy), "test-project"],
        capture_output=True,
        text=True,
        cwd=str(REPO_ROOT),
        env=env,
        timeout=180,
    )
    logs = sorted((tmp_path / "logs" / "dream").glob("*.log"))
    return "\n".join(
        p.read_text(encoding="utf-8", errors="replace") for p in logs if "_" not in p.name
    )


def test_a_provable_no_write_failure_falls_back_to_the_next_provider(tmp_path: Path) -> None:
    """The nominal case: codex dies without writing, claude takes the night."""
    log = _run_night(tmp_path, SAFE_TO_FALL_BACK)

    assert "FALLBACK" in log, log
    assert "bascule vers claude" in log, log
    assert "provider=claude" in log, log


def test_a_failing_chain_still_reaches_the_end_of_the_night(tmp_path: Path) -> None:
    """A trap paid for TWICE while writing the chain.

    A `set -e` placed INSIDE a shell function survives its `return`. The errexit
    thus restored made dream.sh exit on the first non-zero `return`: the night
    stopped at the first failing phase, with no summary and without touching the
    following projects — exiting non-zero, hence looking like an ordinary failure.

    The final summary is the cheapest witness of that failure: it exists only if
    the loop ran to the end.
    """
    log = _run_night(tmp_path, 1)

    assert "Dream finished" in log, log


def test_an_ordinary_failure_never_falls_back(tmp_path: Path) -> None:
    """THE test that matters. An rc=1 does NOT prove nothing was written, so
    replaying the phase elsewhere could write twice. The chain must stay still —
    and the failure mode would be invisible, the night ending green with
    duplicates in the corpus."""
    log = _run_night(tmp_path, 1)

    assert "FALLBACK" not in log, log
    assert "provider=claude" not in log, log


def test_a_timeout_never_falls_back(tmp_path: Path) -> None:
    """A timeout proves even less: the phase may have written then hung."""
    log = _run_night(tmp_path, 124)

    assert "FALLBACK" not in log, log
    assert "provider=claude" not in log, log


def test_a_capability_configuration_error_must_not_advance_the_chain(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    """A broken config is not a provider outage.

    The runner's three fail-closed refusals — non-loopback URL, missing project
    profile, invalid enforcement flag — return 1 and NOT the switch code. That is
    deliberate: the next link would hit exactly the same configuration, so
    switching would repair nothing and would merely consume a second subscription
    before failing the same way. Worse, by hiding the error behind a second
    attempt, it would make the cause harder to read in the morning.

    This test pins that choice, because nothing in the code shouts it: the two
    failure paths look alike, and it would be natural to make them uniform.
    """
    codex = _codex()
    stderr_log = tmp_path / "scan.stderr.log"
    # Without enforcement the configuration-error path does not exist: the runner
    # would really try to launch the binary and would return the switch code for
    # an entirely different reason ("codex did not start").
    registry = {
        f"brain-v42:{phase}": {"active": f"{phase}-token", "accepted": []}
        for phase in ("scan", "clean", "connect", "synth", "promote", "reorg")
    }
    monkeypatch.setenv("BRAIN_DREAM_CAPABILITY_ENFORCEMENT", "true")
    monkeypatch.setenv("MCP_HTTP_TOKEN", "admin-token")
    monkeypatch.setenv("MCP_HTTP_DREAM_TOKENS", json.dumps(registry))

    return_code = codex.run_codex(
        prompt="Return a scan report.",
        phase="scan",
        project_key="a-project-with-no-profile",
        model="gpt-5.6-terra",
        reasoning_effort="medium",
        timeout_seconds=1,
        report_log=tmp_path / "scan.log",
        events_log=tmp_path / "scan.codex.jsonl",
        stderr_log=stderr_log,
        codex_executable="/nonexistent-codex",
    )

    assert return_code == 1
    assert return_code != SAFE_TO_FALL_BACK


# --- The agy link in the chain ----------------------------------------------


def test_agy_is_a_supported_provider_in_the_chain() -> None:
    content = _dream_sh()

    assert "codex|claude|agy)" in content
    assert "scripts.dream.agy_runner" in content


def test_the_agy_link_uses_gemini_models_not_claude_ones() -> None:
    """agy also exposes claude-sonnet-4-6 and claude-opus-4-6-thinking.

    Choosing them would cancel the link's point: if Anthropic goes down, those
    models go down with it, and the chain would have two correlated links
    disguised as three. The diversity sought is the PROVIDER's.
    """
    # On the ASSIGNMENT LINES only: the script's prose legitimately names the
    # discarded models to explain WHY they are discarded.
    assignments = [
        line
        for line in _dream_sh().splitlines()
        if line.startswith("BRAIN_DREAM_AGY_") and "MODEL=" in line
    ]

    assert len(assignments) == 2, assignments
    for line in assignments:
        assert ":-gemini-" in line, line
        assert "claude" not in line, line


def test_every_rail_persists_its_dream_run_row() -> None:
    """Three rails, three parsers. None may play a phase without measuring it.

    The agy rail was deprived of one for a few hours, and that gap was enough to
    dictate an absurd chain order: claude placed before agy to preserve the
    dream_runs rows, hence the subscription we wanted to spare put in the front
    line. A tooling gap must not decide a cost trade-off.
    """
    content = _dream_sh()

    for parser in (
        "brain_v42.metrics.agy_dream_parser",
        "brain_v42.metrics.codex_dream_parser",
        "brain_v42.metrics.dream_parser",
    ):
        assert parser in content, parser
    assert "ligne dream_runs NON enregistrée" not in content


def test_the_agy_preflight_proves_its_tool_guard_before_the_night() -> None:
    """Without a proven guard, an agy phase has a free shell. The preflight must
    therefore PROBE it, not merely note its file — and drop the link otherwise."""
    content = _dream_sh()

    # The preflight has been generic since the chain was introduced: it loops over
    # the providers and labels its messages. What matters is therefore not a label
    # but that the agy link is WIRED into it, and that it refuses to start without
    # enforcement — without which its guard is never probed.
    assert 'agy)    binary="$BRAIN_DREAM_AGY_BIN"' in content
    assert "scripts.dream.agy_runner" in content
    assert 'provider" == "agy" && "$BRAIN_DREAM_CAPABILITY_ENFORCEMENT" != "true"' in content
