"""Contract tests for the isolated Codex Dream phase runner.

The runner is intentionally a small process boundary: it owns the hardened
``codex exec`` command, exact per-phase MCP allowlists, log separation and the
wall-clock timeout.  ``dream.sh`` should not have to reproduce those details.
"""

from __future__ import annotations

import importlib
import importlib.util
import inspect
import json
import os
import shlex
import signal
import time
import tomllib
from pathlib import Path
from types import ModuleType

import pytest
from scripts.dream._agent_capability import PROVIDER_FALLBACK_EXIT_CODE

REPO_ROOT = Path(__file__).resolve().parents[2]
RUNNER_PATH = REPO_ROOT / "scripts" / "dream" / "codex_runner.py"
PROMPT_DIR = REPO_ROOT / "scripts" / "dream"

EXPECTED_PHASE_TOOLS = {
    "scan": (
        "brain_decay_status",
        "brain_consolidation_candidates",
        "brain_list",
        "brain_search",
    ),
    "clean": (
        "brain_search",
        "brain_get",
        "brain_consolidation_candidates",
        "brain_decay_status",
        "brain_merge_entities",
        "brain_delete",
        "brain_list",
    ),
    "connect": (
        "brain_backfill_links_batch",
        "brain_list_orphans_for_classification",
        "brain_assign_domain",
    ),
    "synth": (
        "brain_get_clusters",
        "brain_get",
        "brain_learn",
        "brain_save_snippet",
        "brain_search",
        "brain_list",
        "brain_get_neighbors",
        "brain_graph_path",
    ),
    "promote": (
        "brain_get",
        "brain_search",
        "brain_propose_adr",
        "brain_create_runbook",
        "brain_list_adrs",
        "brain_list",
        "brain_get_neighbors",
        "brain_graph_path",
    ),
    "reorg": (
        "brain_search",
        "brain_list",
        "brain_get",
        "brain_update",
    ),
}


def _runner() -> ModuleType:
    assert RUNNER_PATH.is_file(), (
        "Codex Dream runner is missing: expected scripts/dream/codex_runner.py"
    )
    return importlib.import_module("scripts.dream.codex_runner")


def _config_overrides(command: list[str]) -> dict[str, str]:
    overrides: dict[str, str] = {}
    for index, argument in enumerate(command):
        if argument != "-c":
            continue
        key, value = command[index + 1].split("=", 1)
        overrides[key] = value
    return overrides


def _toml_value(raw_value: str) -> object:
    return tomllib.loads(f"value = {raw_value}")["value"]


def _fake_codex(
    tmp_path: Path,
    *,
    name: str,
    events: tuple[dict[str, object], ...],
    report: str | None,
) -> Path:
    executable = tmp_path / name
    script = [
        "#!/usr/bin/env bash\n",
        "report=''\n",
        "while (($#)); do\n",
        "  if [[ $1 == --output-last-message ]]; then report=$2; shift 2; else shift; fi\n",
        "done\n",
    ]
    if report is not None:
        script.append(f"printf '%s\\n' {shlex.quote(report)} > \"$report\"\n")
    for event in events:
        script.append(f"printf '%s\\n' {shlex.quote(json.dumps(event))}\n")
    executable.write_text("".join(script), encoding="utf-8")
    executable.chmod(0o755)
    return executable


def _completed_event() -> dict[str, object]:
    return {
        "type": "turn.completed",
        "usage": {
            "input_tokens": 20,
            "cached_input_tokens": 5,
            "output_tokens": 4,
        },
    }


def _completed_brain_tool_event() -> dict[str, object]:
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


def _capability_registry(*, project_key: str = "brain-v42") -> str:
    profiles = {
        f"{project_key}:{phase}": {
            "active": f"{phase}-active-token",
            "accepted": [f"{phase}-accepted-token"] if phase == "scan" else [],
        }
        for phase in EXPECTED_PHASE_TOOLS
    }
    return json.dumps(profiles)


def _capture_popen_environment(
    runner: ModuleType,
    monkeypatch: pytest.MonkeyPatch,
) -> dict[str, object]:
    captured: dict[str, object] = {}

    def capture_popen(*args: object, **kwargs: object) -> object:
        captured["args"] = args
        captured["kwargs"] = kwargs
        raise OSError("intentional test stop after Popen capture")

    monkeypatch.setattr(runner.subprocess, "Popen", capture_popen)
    return captured


def _assert_project_scoped_runner_api(runner: ModuleType) -> None:
    assert "project_key" in inspect.signature(runner.run_codex).parameters


def test_phase_tool_allowlists_match_the_six_phase_prompts_exactly() -> None:
    runner = _runner()

    assert runner.PHASE_TOOL_ALLOWLISTS == EXPECTED_PHASE_TOOLS
    assert all("*" not in tool for tools in runner.PHASE_TOOL_ALLOWLISTS.values() for tool in tools)


def test_runner_exposes_the_shared_phase_policy_as_its_compatibility_alias() -> None:
    spec = importlib.util.find_spec("brain_v42.mcp.dream_capabilities")
    assert spec is not None, "the shared Dream capability policy module must exist"
    capabilities = importlib.import_module("brain_v42.mcp.dream_capabilities")
    runner = _runner()

    assert runner.PHASE_TOOL_ALLOWLISTS is capabilities.DREAM_PHASE_TOOL_ALLOWLISTS


def test_connect_dry_run_requires_a_read_only_brain_probe() -> None:
    prompt = (PROMPT_DIR / "phase_connect.md").read_text(encoding="utf-8")

    assert "brain_list_orphans_for_classification(limit=1)" in prompt
    assert "read-only readiness probe" in prompt


def test_promote_reads_the_source_learning_before_classification() -> None:
    prompt = (PROMPT_DIR / "phase_promote.md").read_text(encoding="utf-8")
    probe = 'brain_get(entity_type="learning", entity_id=candidates[0].id)'

    assert probe in prompt
    assert prompt.index(probe) < prompt.index("2. Classify `target_type`")


def test_reorg_verifies_tag_majority_outside_the_scan_window() -> None:
    prompt = (PROMPT_DIR / "phase_reorg.md").read_text(encoding="utf-8")

    assert "same `project_key`" in prompt
    assert 'brain_list(entity_type="learning", project_key=' in prompt
    assert 'brain_list(entity_type="decision", project_key=' in prompt
    assert "include_archived=True" in prompt
    assert "at least twice" in prompt
    assert 'record the pair under "Flagged only" as ambiguous' in prompt


def test_build_command_is_headless_ephemeral_and_read_only(tmp_path: Path) -> None:
    runner = _runner()
    report_log = tmp_path / "scan.log"
    workspace = tmp_path / "empty-workspace"
    workspace.mkdir()

    command = runner.build_codex_command(
        phase="scan",
        model="gpt-5.6-terra",
        reasoning_effort="medium",
        report_log=report_log,
        workspace=workspace,
    )

    assert command[:2] == ["codex", "exec"]
    assert command[-1] == "-", "the potentially large prompt must be read from stdin"
    assert ["--model", "gpt-5.6-terra"] == command[
        command.index("--model") : command.index("--model") + 2
    ]
    assert ["--sandbox", "read-only"] == command[
        command.index("--sandbox") : command.index("--sandbox") + 2
    ]
    assert ["-C", str(workspace)] == command[command.index("-C") : command.index("-C") + 2]
    assert ["--output-last-message", str(report_log)] == command[
        command.index("--output-last-message") : command.index("--output-last-message") + 2
    ]
    assert {
        "--ephemeral",
        "--json",
        "--skip-git-repo-check",
        "--ignore-user-config",
        "--strict-config",
        "--ignore-rules",
    }.issubset(command)


def test_build_command_disables_ambient_capabilities_and_requires_chatgpt_auth(
    tmp_path: Path,
) -> None:
    runner = _runner()

    command = runner.build_codex_command(
        phase="scan",
        model="gpt-5.6-terra",
        reasoning_effort="medium",
        report_log=tmp_path / "scan.log",
        workspace=tmp_path,
    )
    overrides = _config_overrides(command)

    expected_values = {
        "forced_login_method": "chatgpt",
        "approval_policy": "never",
        "model_reasoning_effort": "medium",
        "project_doc_max_bytes": 0,
        "web_search": "disabled",
        "features.shell_tool": False,
        "features.multi_agent": False,
        "features.skill_mcp_dependency_install": False,
        "features.hooks": False,
        "features.goals": False,
        "features.memories": False,
        "apps._default.enabled": False,
        "memories.use_memories": False,
        "memories.generate_memories": False,
        "mcp_servers.brain-v42.required": True,
        "mcp_servers.brain-v42.default_tools_approval_mode": "approve",
        "mcp_servers.brain-v42.bearer_token_env_var": "MCP_HTTP_TOKEN",
        "mcp_servers.brain-v42.http_headers": {
            "X-Brain-Agent": "dream-codex-scan",
            "X-Brain-Tool-Profile": "native",
        },
    }
    for key, expected in expected_values.items():
        assert key in overrides, f"missing hardened Codex config override: {key}"
        assert _toml_value(overrides[key]) == expected
    assert all(
        _toml_value(overrides[f"features.{feature}"]) is False
        for feature in runner._DISABLED_FEATURES
    )


def test_build_command_keeps_the_code_mode_host_mcp_dispatch_needs(tmp_path: Path) -> None:
    """``code_mode_host`` is dispatch plumbing, not an agent capability.

    Since Codex 0.147.0 the ``gpt-5.6-*`` models route every MCP tool call
    through the code-mode host, and there is no direct surface to fall back to:
    measured 2026-08-17, disabling the host fails the dispatch closed with
    ``code-mode host is disabled`` and yields ZERO completed Brain tool calls —
    60 Codex phases, 60 failures, the whole night silently carried by the agy
    fallback while systemd reported ``63/63 phases OK``.

    Turning it off therefore buys no isolation; it only blinds the primary rail.
    The bound that does hold is kept elsewhere and asserted here: the JS REPL
    stays restricted to tool calls, and the reachable tools remain the phase
    allowlist plus the server-side capability scope.
    """
    runner = _runner()

    command = runner.build_codex_command(
        phase="scan",
        model="gpt-5.6-terra",
        reasoning_effort="medium",
        report_log=tmp_path / "scan.log",
        workspace=tmp_path,
    )
    overrides = _config_overrides(command)

    assert "code_mode_host" not in runner._DISABLED_FEATURES
    assert "features.code_mode_host" not in overrides
    assert _toml_value(overrides["features.js_repl_tools_only"]) is True


def test_build_command_passes_only_the_selected_phase_allowlist(tmp_path: Path) -> None:
    runner = _runner()

    for phase, expected_tools in EXPECTED_PHASE_TOOLS.items():
        command = runner.build_codex_command(
            phase=phase,
            model="gpt-5.6-terra",
            reasoning_effort="medium",
            report_log=tmp_path / f"{phase}.log",
            workspace=tmp_path,
        )
        overrides = _config_overrides(command)
        configured_tools = _toml_value(overrides["mcp_servers.brain-v42.enabled_tools"])

        assert tuple(configured_tools) == expected_tools


def test_build_command_rejects_an_unknown_phase_before_other_invalid_options(
    tmp_path: Path,
) -> None:
    runner = _runner()

    with pytest.raises(ValueError, match="unsupported Dream phase: unknown"):
        runner.build_codex_command(
            phase="unknown",
            model="",
            reasoning_effort="unsupported",
            report_log=tmp_path / "unknown.log",
            workspace=tmp_path,
        )


def test_timeout_terminates_the_whole_codex_process_group(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    runner = _runner()
    monkeypatch.setenv("MCP_HTTP_TOKEN", "test-only-token")
    pid_file = tmp_path / "pids"
    fake_codex = tmp_path / "fake-codex"
    fake_codex.write_text(
        "#!/usr/bin/env bash\n"
        "sleep 2 &\n"
        "child=$!\n"
        f'printf \'%s %s\\n\' "$$" "$child" > {shlex.quote(str(pid_file))}\n'
        'wait "$child"\n',
        encoding="utf-8",
    )
    fake_codex.chmod(0o755)

    parent_pid: int | None = None
    started = time.monotonic()
    try:
        return_code = runner.run_codex(
            prompt="Return a scan report.",
            phase="scan",
            model="gpt-5.6-terra",
            reasoning_effort="medium",
            timeout_seconds=0.05,
            report_log=tmp_path / "scan.log",
            events_log=tmp_path / "scan.codex.jsonl",
            stderr_log=tmp_path / "scan.stderr.log",
            codex_executable=str(fake_codex),
        )
        elapsed = time.monotonic() - started
        assert return_code == 124
        assert elapsed < 1.0, "an orphaned child kept the runner alive after timeout"
        assert pid_file.is_file(), "the fake Codex process did not start"
        parent_pid = int(pid_file.read_text(encoding="utf-8").split()[0])

        deadline = time.monotonic() + 1.0
        while time.monotonic() < deadline:
            try:
                os.killpg(parent_pid, 0)
            except ProcessLookupError:
                break
            time.sleep(0.01)
        else:
            raise AssertionError("Codex process group is still alive after timeout")
    finally:
        if parent_pid is not None:
            try:
                os.killpg(parent_pid, signal.SIGKILL)
            except ProcessLookupError:
                pass


def test_timeout_kills_a_child_that_ignores_term_after_the_leader_exits(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    runner = _runner()
    # The process-group terminator is shared with the Claude rail, so the grace
    # period is patched where it now lives rather than on this module.
    capability = importlib.import_module("scripts.dream._agent_capability")
    monkeypatch.setenv("MCP_HTTP_TOKEN", "test-only-token")
    monkeypatch.setattr(capability, "TERMINATION_GRACE_SECONDS", 0.05)
    pid_file = tmp_path / "forked-pids"
    fake_codex = tmp_path / "fake-codex-fork"
    fake_codex.write_text(
        "#!/usr/bin/env python3\n"
        "import os, signal, time\n"
        "child = os.fork()\n"
        "if child == 0:\n"
        "    signal.signal(signal.SIGTERM, signal.SIG_IGN)\n"
        "else:\n"
        f"    open({str(pid_file)!r}, 'w').write(f'{{os.getpid()}} {{child}}\\n')\n"
        "while True:\n"
        "    time.sleep(1)\n",
        encoding="utf-8",
    )
    fake_codex.chmod(0o755)

    return_code = runner.run_codex(
        prompt="Return a scan report.",
        phase="scan",
        model="gpt-5.6-terra",
        reasoning_effort="medium",
        timeout_seconds=0.05,
        report_log=tmp_path / "scan.log",
        events_log=tmp_path / "scan.codex.jsonl",
        stderr_log=tmp_path / "scan.stderr.log",
        codex_executable=str(fake_codex),
    )

    assert return_code == 124
    child_pid = int(pid_file.read_text(encoding="utf-8").split()[1])
    deadline = time.monotonic() + 1
    while time.monotonic() < deadline:
        try:
            os.kill(child_pid, 0)
        except ProcessLookupError:
            break
        time.sleep(0.01)
    else:
        raise AssertionError("SIGTERM-ignoring Codex child survived timeout cleanup")


def test_run_clears_a_stale_report_before_validating_success(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    runner = _runner()
    monkeypatch.setenv("MCP_HTTP_TOKEN", "test-only-token")
    report_log = tmp_path / "scan.log"
    report_log.write_text("stale report from a previous attempt\n", encoding="utf-8")
    fake_codex = _fake_codex(
        tmp_path,
        name="fake-codex-stale",
        events=(_completed_event(),),
        report=None,
    )

    return_code = runner.run_codex(
        prompt="Return a scan report.",
        phase="scan",
        model="gpt-5.6-terra",
        reasoning_effort="medium",
        timeout_seconds=1,
        report_log=report_log,
        events_log=tmp_path / "scan.codex.jsonl",
        stderr_log=tmp_path / "scan.stderr.log",
        codex_executable=str(fake_codex),
    )

    # Sorti 0 sans rapport, et sans aucun appel d'outil abouti : rejouable.
    assert return_code == PROVIDER_FALLBACK_EXIT_CODE
    assert report_log.read_text(encoding="utf-8") == ""


def test_run_accepts_a_fresh_report_and_valid_usage(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    runner = _runner()
    monkeypatch.setenv("MCP_HTTP_TOKEN", "test-only-token")
    fake_codex = _fake_codex(
        tmp_path,
        name="fake-codex-success",
        events=(_completed_brain_tool_event(), _completed_event()),
        report="fresh scan report",
    )

    return_code = runner.run_codex(
        prompt="Return a scan report.",
        phase="scan",
        model="gpt-5.6-terra",
        reasoning_effort="medium",
        timeout_seconds=1,
        report_log=tmp_path / "scan.log",
        events_log=tmp_path / "scan.codex.jsonl",
        stderr_log=tmp_path / "scan.stderr.log",
        codex_executable=str(fake_codex),
    )

    assert return_code == 0


def test_run_rejects_completed_turn_without_a_completed_brain_tool_call(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    runner = _runner()
    monkeypatch.setenv("MCP_HTTP_TOKEN", "test-only-token")
    fake_codex = _fake_codex(
        tmp_path,
        name="fake-codex-no-brain-tools",
        events=(
            {
                "type": "item.completed",
                "item": {
                    "id": "item-1",
                    "type": "mcp_tool_call",
                    "server": "codex",
                    "tool": "list_mcp_resources",
                    "status": "completed",
                },
            },
            _completed_event(),
        ),
        report="Status: BLOCKED because Brain tools are unavailable",
    )

    stderr_log = tmp_path / "scan.stderr.log"
    return_code = runner.run_codex(
        prompt="Return a scan report.",
        phase="scan",
        model="gpt-5.6-terra",
        reasoning_effort="medium",
        timeout_seconds=1,
        report_log=tmp_path / "scan.log",
        events_log=tmp_path / "scan.codex.jsonl",
        stderr_log=stderr_log,
        codex_executable=str(fake_codex),
    )

    # Zéro appel d'outil abouti : la phase est rejouable ailleurs.
    assert return_code == PROVIDER_FALLBACK_EXIT_CODE
    assert "no completed Brain MCP tool call" in stderr_log.read_text(encoding="utf-8")


def test_run_rejects_a_failed_brain_tool_call_with_no_error_payload(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    runner = _runner()
    monkeypatch.setenv("MCP_HTTP_TOKEN", "test-only-token")
    fake_codex = _fake_codex(
        tmp_path,
        name="fake-codex-failed-brain-tool",
        events=(
            {
                "type": "item.completed",
                "item": {
                    "id": "item-1",
                    "type": "mcp_tool_call",
                    "server": "brain-v42",
                    "tool": "brain_decay_status",
                    "status": "failed",
                    "error": None,
                },
            },
            _completed_event(),
        ),
        report="Status: BLOCKED after the Brain tool failed",
    )

    stderr_log = tmp_path / "scan.stderr.log"
    return_code = runner.run_codex(
        prompt="Return a scan report.",
        phase="scan",
        model="gpt-5.6-terra",
        reasoning_effort="medium",
        timeout_seconds=1,
        report_log=tmp_path / "scan.log",
        events_log=tmp_path / "scan.codex.jsonl",
        stderr_log=stderr_log,
        codex_executable=str(fake_codex),
    )

    # L'appel a ÉCHOUÉ, donc rien n'a été commité — rejouable ailleurs.
    assert return_code == PROVIDER_FALLBACK_EXIT_CODE
    assert "no completed Brain MCP tool call" in stderr_log.read_text(encoding="utf-8")


@pytest.mark.parametrize(
    "events",
    [
        ({"type": "turn.completed"},),
        ({"type": "error", "message": "terminal failure"}, _completed_event()),
        ({"type": "turn.failed", "error": {"message": "terminal failure"}},),
    ],
)
def test_run_rejects_missing_usage_and_terminal_events(
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
    events: tuple[dict[str, object], ...],
) -> None:
    runner = _runner()
    monkeypatch.setenv("MCP_HTTP_TOKEN", "test-only-token")
    fake_codex = _fake_codex(
        tmp_path,
        name="fake-codex-invalid-events",
        events=events,
        report="report that must not make an invalid turn succeed",
    )

    return_code = runner.run_codex(
        prompt="Return a scan report.",
        phase="scan",
        model="gpt-5.6-terra",
        reasoning_effort="medium",
        timeout_seconds=1,
        report_log=tmp_path / "scan.log",
        events_log=tmp_path / "scan.codex.jsonl",
        stderr_log=tmp_path / "scan.stderr.log",
        codex_executable=str(fake_codex),
    )

    # Flux terminal ou usage invalide, sans aucun appel abouti.
    assert return_code == PROVIDER_FALLBACK_EXIT_CODE


def test_enabled_run_sends_only_the_active_phase_token_in_an_allowlisted_child_env(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    runner = _runner()
    raw_registry = _capability_registry()
    monkeypatch.setenv("BRAIN_DREAM_CAPABILITY_ENFORCEMENT", "true")
    monkeypatch.setenv("MCP_HTTP_TOKEN", "admin-token")
    monkeypatch.setenv("MCP_HTTP_DREAM_TOKENS", raw_registry)
    monkeypatch.setenv("TOP_SECRET", "must-not-reach-codex")
    monkeypatch.setenv("BRAIN_DREAM_MCP_URL", "https://localhost:8765/mcp")
    monkeypatch.setenv("HTTPS_PROXY", "http://proxy.example:3128")
    monkeypatch.setenv("NO_PROXY", "existing.internal")
    monkeypatch.setenv("no_proxy", "lower.internal")
    captured = _capture_popen_environment(runner, monkeypatch)
    _assert_project_scoped_runner_api(runner)

    return_code = runner.run_codex(
        prompt="Return a scan report.",
        phase="scan",
        project_key="brain-v42",
        model="gpt-5.6-terra",
        reasoning_effort="medium",
        timeout_seconds=1,
        report_log=tmp_path / "scan.log",
        events_log=tmp_path / "scan.codex.jsonl",
        stderr_log=tmp_path / "scan.stderr.log",
    )

    # Codex n'a jamais démarré (OSError) : rien n'a pu être écrit.
    assert return_code == PROVIDER_FALLBACK_EXIT_CODE
    kwargs = captured["kwargs"]
    assert isinstance(kwargs, dict)
    child_env = kwargs.get("env")
    assert isinstance(child_env, dict)
    assert child_env["MCP_HTTP_TOKEN"] == "scan-active-token"
    assert "scan-accepted-token" not in child_env.values()
    assert "admin-token" not in child_env.values()
    assert "MCP_HTTP_DREAM_TOKENS" not in child_env
    assert "TOP_SECRET" not in child_env
    assert child_env["HTTPS_PROXY"] == "http://proxy.example:3128"
    expected_no_proxy = {
        "existing.internal",
        "lower.internal",
        "127.0.0.1",
        "localhost",
        "::1",
    }
    assert set(child_env["NO_PROXY"].split(",")) == expected_no_proxy
    assert set(child_env["no_proxy"].split(",")) == expected_no_proxy
    assert os.environ["MCP_HTTP_TOKEN"] == "admin-token"
    assert os.environ["MCP_HTTP_DREAM_TOKENS"] == raw_registry
    assert os.environ["TOP_SECRET"] == "must-not-reach-codex"


@pytest.mark.parametrize(
    "mcp_url",
    (
        "https://mcp.example.test/mcp",
        "http://admin:credential@localhost:8765/mcp",
        "file:///tmp/brain-mcp.sock",
        "http://127.0.0.2:8765/mcp",
    ),
)
def test_enabled_run_rejects_non_loopback_mcp_urls_before_popen_without_secret_output(
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
    mcp_url: str,
) -> None:
    runner = _runner()
    raw_registry = _capability_registry()
    monkeypatch.setenv("BRAIN_DREAM_CAPABILITY_ENFORCEMENT", "true")
    monkeypatch.setenv("MCP_HTTP_TOKEN", "admin-token")
    monkeypatch.setenv("MCP_HTTP_DREAM_TOKENS", raw_registry)
    monkeypatch.setenv("BRAIN_DREAM_MCP_URL", mcp_url)
    captured = _capture_popen_environment(runner, monkeypatch)
    stderr_log = tmp_path / "scan.stderr.log"

    return_code = runner.run_codex(
        prompt="Return a scan report.",
        phase="scan",
        project_key="brain-v42",
        model="gpt-5.6-terra",
        reasoning_effort="medium",
        timeout_seconds=1,
        report_log=tmp_path / "scan.log",
        events_log=tmp_path / "scan.codex.jsonl",
        stderr_log=stderr_log,
    )

    assert return_code == 1
    assert captured == {}, "an invalid MCP URL must fail before Popen"
    error = stderr_log.read_text(encoding="utf-8")
    assert error == "Dream capability configuration is invalid\n"
    for secret in (mcp_url, "admin-token", "scan-active-token", raw_registry):
        assert secret not in error


@pytest.mark.parametrize(
    ("supplied_project_key", "registry_project_key"),
    (
        ("brain", "brain-v42"),
        ("brain_v42", "brain-v42"),
        ("red-lab:architect", "red-lab:architect"),
    ),
)
def test_enabled_run_canonicalizes_known_aliases_and_preserves_valid_partitions(
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
    supplied_project_key: str,
    registry_project_key: str,
) -> None:
    runner = _runner()
    monkeypatch.setenv("BRAIN_DREAM_CAPABILITY_ENFORCEMENT", "true")
    monkeypatch.setenv("MCP_HTTP_TOKEN", "admin-token")
    monkeypatch.setenv(
        "MCP_HTTP_DREAM_TOKENS",
        _capability_registry(project_key=registry_project_key),
    )
    monkeypatch.setenv("BRAIN_DREAM_MCP_URL", "http://[::1]:8765/mcp")
    captured = _capture_popen_environment(runner, monkeypatch)

    return_code = runner.run_codex(
        prompt="Return a scan report.",
        phase="scan",
        project_key=supplied_project_key,
        model="gpt-5.6-terra",
        reasoning_effort="medium",
        timeout_seconds=1,
        report_log=tmp_path / "scan.log",
        events_log=tmp_path / "scan.codex.jsonl",
        stderr_log=tmp_path / "scan.stderr.log",
    )

    # Codex n'a jamais démarré (OSError) : rien n'a pu être écrit.
    assert return_code == PROVIDER_FALLBACK_EXIT_CODE
    kwargs = captured.get("kwargs")
    assert isinstance(kwargs, dict), "canonical project scope must reach Popen"
    child_env = kwargs.get("env")
    assert isinstance(child_env, dict)
    assert child_env["MCP_HTTP_TOKEN"] == "scan-active-token"


def test_enabled_run_rejects_a_missing_project_profile_before_popen_without_leaking_secrets(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    runner = _runner()
    raw_registry = _capability_registry(project_key="another-project")
    monkeypatch.setenv("BRAIN_DREAM_CAPABILITY_ENFORCEMENT", "true")
    monkeypatch.setenv("MCP_HTTP_TOKEN", "admin-token")
    monkeypatch.setenv("MCP_HTTP_DREAM_TOKENS", raw_registry)
    popen_called = False

    def reject_popen(*args: object, **kwargs: object) -> object:
        nonlocal popen_called
        popen_called = True
        raise AssertionError("Popen must not run for a missing capability profile")

    monkeypatch.setattr(runner.subprocess, "Popen", reject_popen)
    stderr_log = tmp_path / "scan.stderr.log"
    _assert_project_scoped_runner_api(runner)

    return_code = runner.run_codex(
        prompt="Return a scan report.",
        phase="scan",
        project_key="brain-v42",
        model="gpt-5.6-terra",
        reasoning_effort="medium",
        timeout_seconds=1,
        report_log=tmp_path / "scan.log",
        events_log=tmp_path / "scan.codex.jsonl",
        stderr_log=stderr_log,
    )

    assert return_code == 1
    assert popen_called is False
    error = stderr_log.read_text(encoding="utf-8")
    assert error == "Dream capability configuration is invalid\n"
    assert "admin-token" not in error
    assert "scan-active-token" not in error
    assert raw_registry not in error


def test_disabled_run_preserves_popen_environment_inheritance(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    runner = _runner()
    monkeypatch.setenv("BRAIN_DREAM_CAPABILITY_ENFORCEMENT", "false")
    monkeypatch.setenv("MCP_HTTP_TOKEN", "admin-token")
    monkeypatch.setenv("TOP_SECRET", "historically-inherited")
    captured = _capture_popen_environment(runner, monkeypatch)

    return_code = runner.run_codex(
        prompt="Return a scan report.",
        phase="scan",
        model="gpt-5.6-terra",
        reasoning_effort="medium",
        timeout_seconds=1,
        report_log=tmp_path / "scan.log",
        events_log=tmp_path / "scan.codex.jsonl",
        stderr_log=tmp_path / "scan.stderr.log",
    )

    # Codex n'a jamais démarré (OSError) : rien n'a pu être écrit.
    assert return_code == PROVIDER_FALLBACK_EXIT_CODE
    kwargs = captured["kwargs"]
    assert isinstance(kwargs, dict)
    assert "env" not in kwargs


def test_invalid_enforcement_flag_fails_before_popen(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    runner = _runner()
    monkeypatch.setenv("BRAIN_DREAM_CAPABILITY_ENFORCEMENT", "TRUE")
    monkeypatch.setenv("MCP_HTTP_TOKEN", "admin-token")
    popen_called = False

    def reject_popen(*args: object, **kwargs: object) -> object:
        nonlocal popen_called
        popen_called = True
        raise AssertionError("Popen must not run for an invalid enforcement flag")

    monkeypatch.setattr(runner.subprocess, "Popen", reject_popen)
    stderr_log = tmp_path / "scan.stderr.log"
    _assert_project_scoped_runner_api(runner)

    return_code = runner.run_codex(
        prompt="Return a scan report.",
        phase="scan",
        project_key="brain-v42",
        model="gpt-5.6-terra",
        reasoning_effort="medium",
        timeout_seconds=1,
        report_log=tmp_path / "scan.log",
        events_log=tmp_path / "scan.codex.jsonl",
        stderr_log=stderr_log,
    )

    assert return_code == 1
    assert popen_called is False
    assert stderr_log.read_text(encoding="utf-8") == ("Dream capability configuration is invalid\n")


def test_capability_preflight_validates_the_complete_project_without_secret_output(
    monkeypatch: pytest.MonkeyPatch, capsys: pytest.CaptureFixture[str]
) -> None:
    runner = _runner()
    monkeypatch.setenv("BRAIN_DREAM_CAPABILITY_ENFORCEMENT", "true")
    monkeypatch.setenv("MCP_HTTP_TOKEN", "admin-token")
    monkeypatch.setenv("MCP_HTTP_DREAM_TOKENS", _capability_registry())

    try:
        return_code = runner.main(["--preflight-capabilities", "--project-key", "brain-v42"])
    except SystemExit as exc:
        return_code = int(exc.code or 0)

    captured = capsys.readouterr()
    assert return_code == 0
    assert captured.out == ""
    assert captured.err == ""


@pytest.mark.parametrize("project_alias", ("brain", "brain_v42"))
def test_capability_preflight_canonicalizes_known_project_aliases(
    monkeypatch: pytest.MonkeyPatch,
    capsys: pytest.CaptureFixture[str],
    project_alias: str,
) -> None:
    runner = _runner()
    monkeypatch.setenv("BRAIN_DREAM_CAPABILITY_ENFORCEMENT", "true")
    monkeypatch.setenv("MCP_HTTP_TOKEN", "admin-token")
    monkeypatch.setenv("MCP_HTTP_DREAM_TOKENS", _capability_registry())
    monkeypatch.setenv("BRAIN_DREAM_MCP_URL", "http://127.0.0.1:8765/mcp")

    return_code = runner.main(["--preflight-capabilities", "--project-key", project_alias])

    captured = capsys.readouterr()
    assert return_code == 0
    assert captured.out == ""
    assert captured.err == ""
