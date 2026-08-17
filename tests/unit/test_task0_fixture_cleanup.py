"""Contract for the disposable PostgreSQL 16 fixture source."""

from __future__ import annotations

import subprocess
from pathlib import Path

REPOSITORY_ROOT = Path(__file__).resolve().parents[2]
FIXTURE = REPOSITORY_ROOT / "scripts" / "run_task0_fixture.sh"
IMPLEMENTATION = REPOSITORY_ROOT / "tests" / "support" / "run_task0_fixture_impl.sh"
COMPOSE_FILE = REPOSITORY_ROOT / "tests" / "support" / "task0-compose.yml"
PROBE = REPOSITORY_ROOT / "tests" / "support" / "task0_probe.py"


def test_fixture_exports_the_bounded_runner() -> None:
    """The integration gate is sourced, rather than copied into each caller."""
    completed = subprocess.run(
        ["bash", "-c", 'source "$1" && declare -F run_task0_fixture', "bash", str(FIXTURE)],
        capture_output=True,
        text=True,
        check=False,
    )

    assert completed.returncode == 0, completed.stderr


def test_fixture_has_bounded_container_and_cleanup_guards() -> None:
    """A database fixture must not leave a Docker child or volume behind."""
    entrypoint = FIXTURE.read_text(encoding="utf-8")
    source = IMPLEMENTATION.read_text(encoding="utf-8")

    assert 'source "$SCRIPT_DIR/../tests/support/run_task0_fixture_impl.sh"' in entrypoint
    assert "timeout -s KILL 30s" in source
    assert "cleanup_pg16()" in source
    assert "task0_process_group_is_dead()" in source
    assert "task0_prepare()" in source
    assert 'test ! -s "$volumes"' in source
    assert "docker volume " not in source
    assert "docker run " not in source
    assert "docker exec " not in source
    assert "docker port " not in source
    assert COMPOSE_FILE.is_file()
    assert PROBE.is_file()
    assert "bash -ceu" not in source
    assert "DB_STATUS=CLEANUP_FAILED" in source
    assert (
        "pgvector/pgvector:pg16@sha256:1d533553fefe4f12e5d80c7b80622ba0c382abb5758856f52983d8789179f0fb"
        in source
    )
