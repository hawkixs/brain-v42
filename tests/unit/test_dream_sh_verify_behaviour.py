"""Exercise the verify step in a full night with deterministic external commands."""

from __future__ import annotations

import subprocess
from pathlib import Path

import pytest

from tests.unit.test_dream_provider_chain import REPO_ROOT, _sandbox


def _night(
    tmp_path: Path, *, verify_rc: int = 0, enabled: bool = True, dry_run: bool = True
) -> tuple[subprocess.CompletedProcess[str], str, str, list[str], list[str]]:
    dream_copy, env = _sandbox(tmp_path, 0)
    record = tmp_path / "invocations.txt"
    argv = tmp_path / "verify-argv.txt"
    uv_stub = tmp_path / "bin" / "uv"
    uv_stub.write_text(
        "#!/usr/bin/env bash\n"
        'case "$*" in\n'
        "  *brain_v42.maintenance.claim_verify*)\n"
        '    printf "verify\\n" >> "$PHASE_RECORD"\n'
        "    shift 4\n"
        '    printf "%s\\n" "$@" > "$VERIFY_ARGV"\n'
        '    exit "$VERIFY_RC" ;;\n'
        "  *scripts.ticket_extract*)\n"
        '    printf "extract\\n" >> "$PHASE_RECORD"; exit 0 ;;\n'
        "  *brain_v42.maintenance.session_sweep*)\n"
        '    printf "sweep\\n" >> "$PHASE_RECORD"; exit 0 ;;\n'
        "  *brain_v42.agents.run_phase_chain*)\n"
        '    printf "pool\\n" >> "$PHASE_RECORD"\n'
        "    while (($#)); do\n"
        '      if [[ "$1" == "--result-json" ]]; then\n'
        '        printf \'{"fallbacks":[],"dead_links":[]}\\n\' > "$2"\n'
        "        break\n"
        "      fi\n"
        "      shift\n"
        "    done\n"
        "    exit 0 ;;\n"
        "esac\n"
        "exit 0\n",
        encoding="utf-8",
    )
    env.update(
        {
            "BRAIN_DREAM_VERIFY_ENABLED": str(enabled).lower(),
            "BRAIN_DREAM_VERIFY_DRY_RUN": str(dry_run).lower(),
            "BRAIN_DREAM_EXTRACT_ENABLED": "true",
            "BRAIN_DREAM_SWEEP_ENABLED": "true",
            "PHASE_RECORD": str(record),
            "VERIFY_ARGV": str(argv),
            "VERIFY_RC": str(verify_rc),
        }
    )
    result = subprocess.run(
        [str(dream_copy), "test-project"],
        capture_output=True,
        text=True,
        cwd=REPO_ROOT,
        env=env,
        timeout=30,
        check=False,
    )
    log_dir = tmp_path / "logs" / "dream"
    log = next(log_dir.glob("????-??-??.log")).read_text(encoding="utf-8")
    manifest = next(log_dir.glob("*_manifest.tsv")).read_text(encoding="utf-8")
    calls = record.read_text(encoding="utf-8").splitlines() if record.exists() else []
    arguments = argv.read_text(encoding="utf-8").splitlines() if argv.exists() else []
    return result, log, manifest, calls, arguments


@pytest.mark.parametrize("verify_rc", [1, 5, 6])
def test_failed_verify_continues_through_the_night(tmp_path: Path, verify_rc: int) -> None:
    result, log, manifest, calls, _ = _night(tmp_path, verify_rc=verify_rc)

    assert result.returncode == 1, result.stderr
    assert "PREFLIGHT Codex" in log
    assert "PREFLIGHT Claude" in log
    assert "Dream finished" in log
    assert "failed (*/verify)" in log
    assert "DONE extract" in log
    assert "DONE sweep" in log
    assert calls[0] == "verify"
    assert "pool" in calls
    assert calls[-2:] == ["extract", "sweep"]
    assert "failed\tverify\t*" in manifest
    assert "meta\tfinished\t" in manifest


def test_controlled_verify_timeout_keeps_the_unit_green(tmp_path: Path) -> None:
    result, log, manifest, calls, _ = _night(tmp_path, verify_rc=3)

    assert result.returncode == 0, result.stderr
    assert "timed out (*/verify)" in log
    assert "DONE extract" in log and "DONE sweep" in log
    assert calls[-2:] == ["extract", "sweep"]
    assert "timeout\tverify\t*" in manifest
    assert "meta\tfinished\t" in manifest


def test_outer_verify_timeout_reddens_the_unit(tmp_path: Path) -> None:
    result, log, manifest, calls, _ = _night(tmp_path, verify_rc=124)

    assert result.returncode == 1, result.stderr
    assert "outer guard" in log
    assert "timed out (*/verify)" in log
    assert calls[-2:] == ["extract", "sweep"]
    assert "timeout\tverify\t*" in manifest
    assert "meta\tfinished\t" in manifest


@pytest.mark.parametrize("dry_run", [True, False])
def test_successful_verify_passes_only_its_cli_arguments(tmp_path: Path, dry_run: bool) -> None:
    result, log, manifest, calls, arguments = _night(tmp_path, dry_run=dry_run)

    assert result.returncode == 0, result.stderr
    assert "DONE verify" in log
    assert calls[0] == "verify"
    assert "meta\tfinished\t" in manifest
    run_date = next(
        line.split("\t")[2] for line in manifest.splitlines() if line.startswith("meta\trun_date\t")
    )
    expected = [
        "--run-date",
        run_date,
        "--report-dir",
        str(tmp_path / "scripts" / ".." / "logs" / "dream"),
    ]
    if not dry_run:
        expected.append("--wet")
    assert arguments == expected


def test_verify_killswitch_skips_the_cli(tmp_path: Path) -> None:
    result, log, manifest, calls, arguments = _night(tmp_path, enabled=False)

    assert result.returncode == 0, result.stderr
    assert "SKIP verify" in log
    assert "skipped (*/verify" in log
    assert "verify" not in calls
    assert "pool" in calls
    assert calls[-2:] == ["extract", "sweep"]
    assert arguments == []
    assert "skipped\tverify\t*\tkillswitch" in manifest
    assert "meta\tfinished\t" in manifest
