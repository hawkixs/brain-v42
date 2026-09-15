"""The opencode link in ``scripts/dream.sh``: declared, preflighted, exported.

Text pins first (what the script says), then the safety EXECUTED: a real copy
of dream.sh runs with stubbed binaries and a stubbed agents interpreter whose
exit code is chosen PER PROVIDER, so the switch codex -> opencode is observed
in the night's log rather than assumed from the case arm.
"""

from __future__ import annotations

import json
import subprocess
import sys
from pathlib import Path

REPO_ROOT = Path(__file__).resolve().parents[2]
DREAM_SH = REPO_ROOT / "scripts" / "dream.sh"


def _dream_sh() -> str:
    return DREAM_SH.read_text(encoding="utf-8")


# --- Text pins ---------------------------------------------------------------


def test_opencode_is_a_supported_provider_in_both_validations() -> None:
    content = _dream_sh()
    # The historical arm stays as it is (pinned elsewhere); opencode is a
    # separate arm in both the single-provider and the chain validation.
    assert "codex|claude|agy)" in content
    assert content.count("    opencode) ;;") == 2


def test_opencode_binary_and_models_have_subscription_backed_defaults() -> None:
    content = _dream_sh()
    assert 'BRAIN_DREAM_OPENCODE_BIN="${BRAIN_DREAM_OPENCODE_BIN:-opencode}"' in content
    assignments = [
        line
        for line in content.splitlines()
        if line.startswith("BRAIN_DREAM_OPENCODE_") and "MODEL=" in line
    ]
    assert len(assignments) == 2, assignments
    for line in assignments:
        # A Go model, never a contributor one: Meta trains on those prompts.
        assert ":-opencode-go/" in line, line
        assert "contributor" not in line, line
    assert 'BRAIN_DREAM_OPENCODE_FAST_VARIANT="${BRAIN_DREAM_OPENCODE_FAST_VARIANT:-}"' in content
    assert (
        'BRAIN_DREAM_OPENCODE_DEEP_VARIANT="${BRAIN_DREAM_OPENCODE_DEEP_VARIANT:-high}"' in content
    )


def test_the_opencode_preflight_checks_binary_credentials_and_runtime_cache() -> None:
    content = _dream_sh()
    assert 'opencode) binary="$BRAIN_DREAM_OPENCODE_BIN"' in content
    assert 'runner="brain_v42.agents.providers.opencode"' in content
    assert '"$HOME/.local/share/opencode/auth.json"' in content
    assert '"$HOME/.config/opencode/node_modules"' in content


def test_the_python_chain_receives_the_opencode_variables() -> None:
    content = _dream_sh()
    assert "export BRAIN_DREAM_OPENCODE_FAST_MODEL BRAIN_DREAM_OPENCODE_DEEP_MODEL" in content
    assert "export BRAIN_DREAM_OPENCODE_FAST_VARIANT BRAIN_DREAM_OPENCODE_DEEP_VARIANT" in content
    assert "BRAIN_DREAM_OPENCODE_BIN" in content.split("_run_phase_chain_python() {", 1)[1]


# --- The safety, EXECUTED ----------------------------------------------------


def _sandbox(
    tmp_path: Path, exit_codes: dict[str, int], *, seed_home: bool
) -> tuple[Path, dict[str, str]]:
    scripts_dir = tmp_path / "scripts"
    scripts_dir.mkdir()
    dream_copy = scripts_dir / "dream.sh"
    dream_copy.write_text(_dream_sh(), encoding="utf-8")
    dream_copy.chmod(0o755)
    subprocess.run(
        ["cp", "-r", str(REPO_ROOT / "scripts" / "dream"), str(scripts_dir / "dream")],
        check=True,
    )

    home = tmp_path / "home"
    home.mkdir()
    if seed_home:
        (home / ".local/share/opencode").mkdir(parents=True)
        (home / ".local/share/opencode/auth.json").write_text(
            json.dumps({"opencode-go": {"type": "api", "key": "x"}}), encoding="utf-8"
        )
        (home / ".config/opencode/node_modules").mkdir(parents=True)
        (home / ".config/opencode/package.json").write_text("{}", encoding="utf-8")
        (home / ".config/opencode/package-lock.json").write_text("{}", encoding="utf-8")

    mock_bin = tmp_path / "bin"
    mock_bin.mkdir()
    for name in ("claude", "opencode"):
        stub = mock_bin / name
        stub.write_text("#!/usr/bin/env bash\ncat >/dev/null 2>&1 || true\nexit 0\n")
        stub.chmod(0o755)
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

    arms = "".join(
        f"  brain_v42.agents.providers.{provider}) exit {code} ;;\n"
        for provider, code in exit_codes.items()
    )
    fake_python = mock_bin / "fake-agents-python"
    fake_python.write_text(
        "#!/usr/bin/env bash\n"
        "cat >/dev/null 2>&1 || true\n"
        'module=""\n'
        "while (($#)); do\n"
        '  case "$1" in\n'
        '    -m) module="$2"; shift 2 ;;\n'
        "    *) shift ;;\n"
        "  esac\n"
        "done\n"
        'case "$module" in\n'
        f"{arms}"
        "  brain_v42.metrics.otel_split) exit 1 ;;\n"
        "  brain_v42.metrics.*) exit 0 ;;\n"
        "esac\n"
        "exit 0\n"
    )
    fake_python.chmod(0o755)

    uv_stub = mock_bin / "uv"
    uv_stub.write_text(
        "#!/usr/bin/env bash\n"
        "cat >/dev/null 2>&1 || true\n"
        'case "$*" in\n'
        "  *brain_v42.agents.run_phase_chain*)\n"
        "    shift 2\n"
        f'    exec env PYTHONPATH="{REPO_ROOT / "src"}" '
        f'BRAIN_AGENTS_SUBPROCESS_PYTHON="{fake_python}" '
        f'"{sys.executable}" "$@"\n'
        "    ;;\n"
        "esac\n"
        "exit 0\n"
    )
    uv_stub.chmod(0o755)

    env = {
        "HOME": str(home),
        "PATH": f"{mock_bin}:/usr/bin:/bin",
        "XDG_RUNTIME_DIR": str(tmp_path),
        "MCP_HTTP_TOKEN": "test-only-token",
        "BRAIN_DREAM_AGENT_PROVIDERS": "codex,opencode,claude",
    }
    return dream_copy, env


def _run_night(tmp_path: Path, exit_codes: dict[str, int], *, seed_home: bool) -> str:
    dream_copy, env = _sandbox(tmp_path, exit_codes, seed_home=seed_home)
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


def test_codex_dying_without_a_write_hands_the_phase_to_opencode(tmp_path: Path) -> None:
    log = _run_night(tmp_path, {"codex": 3, "opencode": 0, "claude": 0}, seed_home=True)

    assert "PREFLIGHT OpenCode — ready" in log
    assert "Providers (3) prêts, dans l'ordre : codex opencode claude" in log
    assert "provider=opencode" in log
    assert "FALLBACK test-project/scan — codex a échoué" in log
    assert "bascule vers opencode" in log
    # opencode took the phase: claude never had to.
    assert "provider=claude" not in log


def test_a_host_without_the_runtime_cache_drops_the_link_before_the_night(
    tmp_path: Path,
) -> None:
    log = _run_night(tmp_path, {"codex": 3, "opencode": 0, "claude": 0}, seed_home=False)

    assert "FAIL OpenCode preflight" in log
    assert "DROP opencode" in log
    assert "Providers (2) prêts, dans l'ordre : codex claude" in log
    assert "provider=opencode" not in log
    assert "bascule vers claude" in log
