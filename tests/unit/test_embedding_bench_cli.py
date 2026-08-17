from __future__ import annotations

import subprocess
import sys
from pathlib import Path

import pytest

ROOT = Path(__file__).resolve().parents[2]
BENCH_DIR = ROOT / "bench" / "embedding_v1"


def _run_without_anthropic(script: Path, *args: str) -> subprocess.CompletedProcess[str]:
    bootstrap = """
import builtins
import runpy
import sys

real_import = builtins.__import__


def import_without_anthropic(name, *args, **kwargs):
    if name == "anthropic" or name.startswith("anthropic."):
        raise ModuleNotFoundError("No module named 'anthropic'", name="anthropic")
    return real_import(name, *args, **kwargs)


builtins.__import__ = import_without_anthropic
sys.argv = sys.argv[1:]
runpy.run_path(sys.argv[0], run_name="__main__")
"""

    return subprocess.run(
        [sys.executable, "-c", bootstrap, str(script), *args],
        check=False,
        capture_output=True,
        text=True,
    )


@pytest.mark.parametrize("script_name", ["gen_gold.py", "run_bench.py"])
def test_help_does_not_require_anthropic(script_name: str) -> None:
    result = _run_without_anthropic(BENCH_DIR / script_name, "--help")

    assert result.returncode == 0, result.stderr
    assert "usage:" in result.stdout


def test_gen_gold_reports_missing_anthropic_without_traceback() -> None:
    result = _run_without_anthropic(BENCH_DIR / "gen_gold.py")

    assert result.returncode == 2
    assert "anthropic" in result.stderr.lower()
    assert "traceback" not in result.stderr.lower()
