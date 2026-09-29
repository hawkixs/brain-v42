"""Pin the nightly VERIFY shell step before provider work begins."""

import re
import shlex
import subprocess
from pathlib import Path

import pytest

DREAM_SH = Path(__file__).resolve().parents[2] / "scripts" / "dream.sh"


def _source() -> str:
    return DREAM_SH.read_text(encoding="utf-8")


def _verify_block() -> str:
    source = _source()
    start = source.index("# --- VERIFY:")
    end = source.index("# Preflights, run across", start)
    return source[start:end]


def test_counters_are_initialized_once_before_verify() -> None:
    source = _source()
    pool = source.index('log "=== Pool (')
    verify = source.index("# --- VERIFY:")
    for declaration in (
        "declare -a FAILED_PHASES=()",
        "declare -a TIMED_OUT_PHASES=()",
        "declare -a CONTROLLED_TIMEOUT_PHASES=()",
        "declare -a SKIPPED_PHASES=()",
        "SKIPPED_UNWRITTEN=0",
        "declare -a FALLBACK_PHASES=()",
        "declare -a DEAD_LINKS=()",
        "TOTAL_PHASES=0",
    ):
        assert source.count(declaration) == 1
        assert pool < source.index(declaration) < verify
    for name in (
        "FAILED_PHASES",
        "TIMED_OUT_PHASES",
        "CONTROLLED_TIMEOUT_PHASES",
        "SKIPPED_PHASES",
    ):
        assert len(re.findall(rf"^(?:declare -a )?{name}=", source, re.MULTILINE)) == 1
    assert len(re.findall(r"^SKIPPED_UNWRITTEN=0$", source, re.MULTILINE)) == 1
    assert len(re.findall(r"^TOTAL_PHASES=0$", source, re.MULTILINE)) == 1


def test_verify_is_top_level_before_provider_preflight_and_project_loop() -> None:
    source = _source()
    verify = source.index("# --- VERIFY:")
    function_start = source.index("run_project_phases() {")
    function_end = source.index("\n}\n", function_start)
    assert source.index('log "=== Pool (') < verify
    assert verify < source.index("preflight_provider() {")
    assert verify < source.index("done  # fin de la boucle de projets")
    assert not function_start < verify < function_end
    assert source[verify - 1] == "\n"


def test_verify_declares_the_expected_pair_and_killswitch_skip() -> None:
    block = _verify_block()
    assert "TOTAL_PHASES=$(( TOTAL_PHASES + 1 ))" in block
    assert "manifest_put expected verify '*'" in block
    assert 'if [[ "$BRAIN_DREAM_VERIFY_ENABLED" != "true" ]]' in block
    assert "manifest_put skipped verify '*' killswitch" in block
    assert 'SKIPPED_PHASES+=("*/verify")' in block
    assert "SKIPPED_UNWRITTEN=$(( SKIPPED_UNWRITTEN + 1 ))" in block


def test_verify_cli_has_only_its_contract_arguments_and_guard() -> None:
    block = _verify_block()
    assert 'verify_args=(--run-date "$TIMESTAMP" --report-dir "$LOG_DIR")' in block
    assert 'if dream_wants_wet BRAIN_DREAM_VERIFY_DRY_RUN "$BRAIN_DREAM_VERIFY_DRY_RUN"' in block
    assert re.findall(r"verify_args\+=\(([^)]*)\)", block) == ["--wet"]
    assert "timeout 5m uv run python -m brain_v42.maintenance.claim_verify" in block
    assert '"${verify_args[@]}"' in block
    assert "TOKEN" not in block
    assert block.index("set +e") < block.index("timeout 5m uv run") < block.index("set -e")


def test_verify_exit_codes_are_classified_for_the_night() -> None:
    block = _verify_block()
    assert 'case "$verify_rc" in' in block
    for arm in ("0)", "3)", "124)", "5)", "6)", "*)"):
        assert arm in block
    assert 'TIMED_OUT_PHASES+=("*/verify")' in block
    assert 'CONTROLLED_TIMEOUT_PHASES+=("*/verify")' in block
    assert 'FAILED_PHASES+=("*/verify")' in block
    assert "manifest_put timeout verify '*'" in block
    assert "manifest_put failed verify '*'" in block
    assert "FAIL verify (partial: per-claim errors)" in block
    assert "BUSY verify (another wet claim_verify holds the run lock)" in block


@pytest.mark.parametrize(
    ("cli_rc", "classification", "controlled"),
    [
        (0, "done", False),
        (3, "timeout", True),
        (124, "timeout", False),
        (5, "failed", False),
        (6, "failed", False),
        (1, "failed", False),
    ],
)
def test_verify_return_code_does_not_stop_later_work(
    tmp_path: Path, cli_rc: int, classification: str, controlled: bool
) -> None:
    """A failed direct CLI must not trigger the night's top-level errexit."""
    script = "\n".join(
        (
            "set -euo pipefail",
            f"LOG_DIR={shlex.quote(str(tmp_path))}",
            "TIMESTAMP=2026-09-29",
            "BRAIN_DREAM_VERIFY_ENABLED=true",
            "BRAIN_DREAM_VERIFY_DRY_RUN=true",
            "TOTAL_PHASES=0",
            "SKIPPED_UNWRITTEN=0",
            "declare -a FAILED_PHASES=() TIMED_OUT_PHASES=() CONTROLLED_TIMEOUT_PHASES=() SKIPPED_PHASES=()",
            f"CLI_RC={cli_rc}",
            "log() { :; }",
            "dream_wants_wet() { return 1; }",
            'manifest_put() { printf "%s %s %s %s\\n" "$1" "$2" "$3" "${4-}"; }',
            'timeout() { return "$CLI_RC"; }',
            _verify_block(),
            'printf "AFTER_VERIFY total=%s failed=%s timeout=%s controlled=%s skipped=%s\\n" '
            '"$TOTAL_PHASES" "${#FAILED_PHASES[@]}" "${#TIMED_OUT_PHASES[@]}" '
            '"${#CONTROLLED_TIMEOUT_PHASES[@]}" "$SKIPPED_UNWRITTEN"',
        )
    )
    result = subprocess.run(
        ["bash", "-c", script], capture_output=True, text=True, timeout=10, check=False
    )
    assert result.returncode == 0, result.stderr
    assert "expected verify *" in result.stdout
    assert "AFTER_VERIFY total=1" in result.stdout
    if classification == "done":
        assert "failed verify *" not in result.stdout
        assert "timeout verify *" not in result.stdout
    else:
        assert f"{classification} verify *" in result.stdout
    assert f"controlled={int(controlled)}" in result.stdout
