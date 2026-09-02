"""The agy rail — ephemeral HOME, scoped bearer, guard wired per invocation.

agy takes NO configuration on the command line: neither `--mcp-config` nor a tool
allowlist. Its embedded documentation (`docs/mcp_servers.md`) knows only two
locations, both global. Measured: a project-level `.agents/hooks.json` is not
discovered, even in a trusted workspace and a git repository.

The only path that gives PER-INVOCATION control is therefore an ephemeral HOME —
verified on 2026-08-11: agy finds `.gemini/config/{mcp_config.json,hooks.json}`
there and authenticates through the credentials linked from the real HOME.

What it avoids counts as much as what it allows: without it, the rail's security
would rest on a global file outside the repository, that a manual edit or an agy
update could silently remove — and two concurrent phases would tread on each
other by rewriting the same mcp_config.

THE SECRET ON DISK, accepted and bounded. agy's `Authorization` is a LITERAL: its
documentation documents no `${VAR}` interpolation, unlike the repository's
.mcp.json. The phase's bearer is therefore written into a file, where codex and
claude pass it through the environment. It is confined to a 0700 HOME under
XDG_RUNTIME_DIR — a tmpfs, hence never the persistent disk — and destroyed with
it. This is the agy rail's only deviation; it is named here so that it is not
rediscovered by accident.
"""

from __future__ import annotations

import importlib
import json
import os
import stat
from pathlib import Path
from types import ModuleType

import pytest

REPO_ROOT = Path(__file__).resolve().parents[2]
RUNNER_PATH = REPO_ROOT / "scripts" / "dream" / "agy_runner.py"
GUARD_PATH = REPO_ROOT / "scripts" / "dream" / "agy_tool_guard.sh"

PHASES = ("scan", "clean", "connect", "synth", "promote", "reorg")
ADMIN_TOKEN = "admin-token-never-scoped"


def _runner() -> ModuleType:
    assert RUNNER_PATH.is_file(), "attendu : scripts/dream/agy_runner.py"
    return importlib.import_module("scripts.dream.agy_runner")


def _registry(*, project_key: str = "brain-v42") -> str:
    return json.dumps(
        {
            f"{project_key}:{phase}": {"active": f"{phase}-active-token", "accepted": []}
            for phase in PHASES
        }
    )


def _enforced_environment(**overrides: str) -> dict[str, str]:
    environment = {
        "BRAIN_DREAM_CAPABILITY_ENFORCEMENT": "true",
        "MCP_HTTP_TOKEN": ADMIN_TOKEN,
        "MCP_HTTP_DREAM_TOKENS": _registry(),
        "HOME": "/home/hawixs",
        "PATH": "/usr/bin:/bin",
    }
    environment.update(overrides)
    return environment


# --- The ephemeral HOME -----------------------------------------------------


def test_the_ephemeral_home_carries_the_phase_scoped_bearer(tmp_path: Path) -> None:
    runner = _runner()

    home = runner.build_ephemeral_home(
        root=tmp_path,
        phase="scan",
        project_key="brain-v42",
        environ=_enforced_environment(),
        real_home=Path("/home/hawixs"),
    )

    config = json.loads((home / ".gemini" / "config" / "mcp_config.json").read_text())
    authorization = config["mcpServers"]["brain-v42"]["headers"]["Authorization"]

    assert authorization == "Bearer scan-active-token"
    assert ADMIN_TOKEN not in json.dumps(config)


def test_each_phase_gets_a_different_bearer(tmp_path: Path) -> None:
    runner = _runner()

    seen = set()
    for index, phase in enumerate(PHASES):
        home = runner.build_ephemeral_home(
            root=tmp_path / str(index),
            phase=phase,
            project_key="brain-v42",
            environ=_enforced_environment(),
            real_home=Path("/home/hawixs"),
        )
        config = json.loads((home / ".gemini" / "config" / "mcp_config.json").read_text())
        seen.add(config["mcpServers"]["brain-v42"]["headers"]["Authorization"])

    assert len(seen) == len(PHASES), "six phases, six bearers"


def test_the_mcp_config_declares_only_brain_v42_on_loopback(tmp_path: Path) -> None:
    """The real HOME's mcp_config also declares red-writer, on a public URL.
    Copying it as is would give a dream phase an access it has no reason to
    have."""
    runner = _runner()

    home = runner.build_ephemeral_home(
        root=tmp_path,
        phase="scan",
        project_key="brain-v42",
        environ=_enforced_environment(),
        real_home=Path("/home/hawixs"),
    )
    config = json.loads((home / ".gemini" / "config" / "mcp_config.json").read_text())

    assert set(config["mcpServers"]) == {"brain-v42"}
    assert config["mcpServers"]["brain-v42"]["serverUrl"].startswith("http://127.0.0.1:")


def test_the_agent_header_names_the_phase(tmp_path: Path) -> None:
    runner = _runner()

    for phase in PHASES:
        home = runner.build_ephemeral_home(
            root=tmp_path / phase,
            phase=phase,
            project_key="brain-v42",
            environ=_enforced_environment(),
            real_home=Path("/home/hawixs"),
        )
        config = json.loads((home / ".gemini" / "config" / "mcp_config.json").read_text())

        assert config["mcpServers"]["brain-v42"]["headers"]["X-Brain-Agent"] == f"dream-agy-{phase}"


def test_the_secret_bearing_file_is_not_world_readable(tmp_path: Path) -> None:
    """The agy rail's only deviation: the bearer is written, not passed by env."""
    runner = _runner()

    home = runner.build_ephemeral_home(
        root=tmp_path,
        phase="scan",
        project_key="brain-v42",
        environ=_enforced_environment(),
        real_home=Path("/home/hawixs"),
    )
    config_path = home / ".gemini" / "config" / "mcp_config.json"

    assert stat.S_IMODE(config_path.stat().st_mode) == 0o600
    assert stat.S_IMODE(home.stat().st_mode) == 0o700


def test_the_hook_points_at_the_versioned_guard(tmp_path: Path) -> None:
    """The guard must come from the REPOSITORY, not from a copy written next to
    the secret.

    A copy would be editable without review and would diverge from its tests; the
    absolute path to the versioned file keeps the two together.
    """
    runner = _runner()

    home = runner.build_ephemeral_home(
        root=tmp_path,
        phase="scan",
        project_key="brain-v42",
        environ=_enforced_environment(),
        real_home=Path("/home/hawixs"),
    )
    hooks = json.loads((home / ".gemini" / "config" / "hooks.json").read_text())
    entry = next(iter(hooks.values()))
    handler = entry["PreToolUse"][0]

    assert handler["matcher"] == "*", "la garde doit voir TOUS les outils"
    assert str(GUARD_PATH) in handler["hooks"][0]["command"]


def test_credentials_are_linked_not_copied(tmp_path: Path) -> None:
    """Duplicating the user's OAuth tokens would make copies to revoke one by
    one. A link reads the original and dies with the HOME."""
    runner = _runner()
    real_home = tmp_path / "real"
    (real_home / ".gemini" / "antigravity-cli").mkdir(parents=True)
    (real_home / ".gemini" / "oauth_creds.json").write_text("{}")
    (real_home / ".gemini" / "antigravity-cli" / "antigravity-oauth-token").write_text("t")

    home = runner.build_ephemeral_home(
        root=tmp_path / "eph",
        phase="scan",
        project_key="brain-v42",
        environ=_enforced_environment(),
        real_home=real_home,
    )

    linked = home / ".gemini" / "oauth_creds.json"
    assert linked.is_symlink(), "les credentials doivent être liés, jamais copiés"


def test_a_missing_project_profile_fails_closed(tmp_path: Path) -> None:
    runner = _runner()
    from brain_v42.mcp.dream_capabilities import DreamCapabilityConfigurationError

    with pytest.raises(DreamCapabilityConfigurationError):
        runner.build_ephemeral_home(
            root=tmp_path,
            phase="scan",
            project_key="un-projet-sans-profil",
            environ=_enforced_environment(),
            real_home=Path("/home/hawixs"),
        )


# --- The switch predicate ---------------------------------------------------


def _stream_event(**fields: object) -> str:
    return json.dumps(fields) + "\n"


def test_a_completed_mcp_step_is_read_as_a_brain_tool_call(tmp_path: Path) -> None:
    runner = _runner()
    events = tmp_path / "events.jsonl"
    events.write_text(
        _stream_event(event="init", init={"tools": []})
        + _stream_event(
            event="step_update",
            step_update={
                "step_index": 3,
                "step_type": "tool",
                "state": "DONE",
                "tool_name": "call_mcp_tool",
            },
        )
    )

    assert runner.brain_tool_call_completed(events) is True


def test_a_run_without_any_completed_mcp_step_is_safe_to_fall_back(tmp_path: Path) -> None:
    runner = _runner()
    events = tmp_path / "events.jsonl"
    events.write_text(
        _stream_event(event="init", init={"tools": []})
        + _stream_event(
            event="step_update",
            step_update={"step_index": 1, "step_type": "agent_response", "state": "DONE"},
        )
    )

    assert runner.brain_tool_call_completed(events) is False


def test_an_mcp_step_that_errored_did_not_write(tmp_path: Path) -> None:
    runner = _runner()
    events = tmp_path / "events.jsonl"
    events.write_text(
        _stream_event(
            event="step_update",
            step_update={
                "step_index": 3,
                "step_type": "tool",
                "state": "ERROR",
                "tool_name": "call_mcp_tool",
            },
        )
    )

    assert runner.brain_tool_call_completed(events) is False


def test_a_denied_non_mcp_tool_never_counts_as_a_brain_call(tmp_path: Path) -> None:
    """A run_command refused by the guard did produce a tool step. Counting it
    would block the switch on a phase that wrote nothing."""
    runner = _runner()
    events = tmp_path / "events.jsonl"
    events.write_text(
        _stream_event(
            event="step_update",
            step_update={
                "step_index": 2,
                "step_type": "tool",
                "state": "DONE",
                "tool_name": "run_command",
            },
        )
    )

    assert runner.brain_tool_call_completed(events) is False


def test_a_missing_event_stream_does_not_block_the_fallback(tmp_path: Path) -> None:
    runner = _runner()

    assert runner.brain_tool_call_completed(tmp_path / "absent.jsonl") is False


# --- The preflight, which PROVES the guard refuses --------------------------


def test_the_preflight_proves_the_guard_denies_a_shell_tool() -> None:
    """The preflight may not settle for noting that the file exists.

    The guard is the only rampart between a nightly phase and a shell. Checking
    its PRESENCE would let through a guard that is empty, misnamed, non-executable
    or made permissive by an edit — all states in which the file exists.
    """
    runner = _runner()

    assert runner.guard_denies_machine_tools() is True


def test_the_preflight_fails_closed_when_the_guard_is_unusable(tmp_path: Path) -> None:
    runner = _runner()
    broken = tmp_path / "broken_guard.sh"
    broken.write_text('#!/usr/bin/env bash\nprintf \'{"decision":"allow"}\'\n')
    broken.chmod(0o755)

    assert runner.guard_denies_machine_tools(guard=broken) is False
    assert runner.guard_denies_machine_tools(guard=tmp_path / "absent.sh") is False


def test_the_runner_exposes_a_project_scoped_api() -> None:
    import inspect

    parameters = inspect.signature(_runner().run_agy).parameters
    assert "project_key" in parameters
    assert "phase" in parameters


def test_the_ephemeral_home_defaults_to_a_tmpfs_runtime_dir() -> None:
    """The bearer is written to disk: it must not land on persistent storage.
    XDG_RUNTIME_DIR is a tmpfs."""
    runner = _runner()

    root = runner.ephemeral_root({"XDG_RUNTIME_DIR": "/run/user/1001"})
    assert str(root).startswith("/run/user/1001")

    fallback = runner.ephemeral_root({})
    assert fallback is None or str(fallback).startswith(os.environ.get("TMPDIR", "/tmp"))


# --- The prompt: argv, not stdin --------------------------------------------


def test_the_prompt_travels_as_the_print_argument_not_on_stdin() -> None:
    """MEASURED on 2026-08-11: agy ignores stdin, in both forms.

    `--print ""` with the prompt on stdin returns an empty answer, and a prompt in
    an argument PLUS a context on stdin answers without the context. The other two
    rails deliberately go through stdin to avoid ARG_MAX; agy leaves no choice.

    The failure mode if you get it wrong is treacherous: agy answers anyway, with
    a greeting, and the phase exits 0 with an off-topic report.
    """
    runner = _runner()

    command = runner.build_agy_command(model="gemini-3.6-flash-medium", prompt="AUDIT SCAN")

    assert "AUDIT SCAN" in command
    assert command[command.index("--print") + 1] == "AUDIT SCAN"
    assert "-" not in command[command.index("--print") + 1]


def test_an_oversized_prompt_is_refused_with_a_readable_reason() -> None:
    """An argument over 128 KiB -> E2BIG, an opaque OSError deep inside a Popen.
    Refusing it BEFORE, with its size, makes the cause readable in the morning."""
    runner = _runner()

    with pytest.raises(ValueError, match="trop long"):
        runner.build_agy_command(model="gemini-3.6-flash-medium", prompt="x" * 200_000)


def test_no_secret_travels_through_argv() -> None:
    """The prompt is in argv, hence visible in `ps`. The BEARER, by contrast, must
    never be: it lives in the ephemeral HOME's mcp_config."""
    runner = _runner()

    command = runner.build_agy_command(model="gemini-3.6-flash-medium", prompt="AUDIT")

    assert not any("Bearer" in argument for argument in command)
    assert not any("token" in argument.lower() for argument in command)


# --- The phase report -------------------------------------------------------


def test_the_report_is_extracted_from_the_result_event(tmp_path: Path) -> None:
    """dream.sh injects this report into the next phase and gives it to its
    validators. Looking for it in the wrong place produces an EMPTY report and a
    broken dependency chain — with no error, the phase exiting 0."""
    runner = _runner()
    events = tmp_path / "events.jsonl"
    report = tmp_path / "report.log"
    events.write_text(
        json.dumps({"event": "init", "init": {"tools": []}})
        + "\n"
        + json.dumps(
            {"event": "result", "result": {"status": "SUCCESS", "response": "=== SCAN REPORT ==="}}
        )
        + "\n"
    )

    runner.extract_report(events, report)

    assert report.read_text().strip() == "=== SCAN REPORT ==="
