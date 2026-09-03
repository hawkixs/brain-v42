"""The HNSW churn bench, EXERCISED — ticket `404d010d`.

`tests/unit/test_hnsw_churn_bench_cleanup.py` is a textual contract: it greps
`scripts/hnsw_churn_measure.sh` for its guards, its `exit 2`s, its markers and
its guard-before-trap ordering. The PR #41 hotfix hardened those greps, and a
grep still does not prove EXECUTION — a guard that is syntactically present can
be short-circuited by a sequence nobody re-read.

Six proofs were played BY HAND on 2026-08-29 and guarded by no test. This file
replays the ones the ticket names: refusing the production container (1),
refusing a foreign container holding the bench name (3), the nominal
provision → measure → teardown (4), and SIGINT mid-run (5).

PRODUCTION IS NEVER TOUCHED. The script reads its corpus with a `COPY … TO
STDOUT` against `$PROD_CONTAINER`; every test here points that variable at a
DISPOSABLE container carrying a synthetic `learnings` table. `brain_v42_postgres`
is named nowhere below.

THE SIGINT HARNESS TRAP, which the ticket measured and this file obeys: a job
started with `&` from a non-interactive shell begins with SIGINT IGNORED (POSIX),
so the trap is untestable that way. `subprocess.Popen(..., restore_signals=True)`
is what makes the signal deliverable.
"""

from __future__ import annotations

import os
import signal
import subprocess
import time
import uuid
from collections.abc import Iterator
from pathlib import Path

import pytest

pytestmark = pytest.mark.bench_docker

ROOT = Path(__file__).parents[3]
SCRIPT = ROOT / "scripts" / "hnsw_churn_measure.sh"
#: The bench compose pins this digest with `pull_policy: never`; the source
#: container uses the same image so the test pulls nothing either.
IMAGE = (
    "pgvector/pgvector:pg16@sha256:1d533553fefe4f12e5d80c7b80622ba0c382abb5758856f52983d8789179f0fb"
)
CORPUS_ROWS = 120
DIM = 1536


def _docker(*args: str, check: bool = True) -> subprocess.CompletedProcess[str]:
    return subprocess.run(
        ["docker", *args], capture_output=True, text=True, check=check, timeout=180
    )


def _names(pattern: str) -> list[str]:
    out = _docker("ps", "-aq", "--filter", f"name={pattern}", check=False).stdout.split()
    return out


def _networks(pattern: str) -> list[str]:
    return _docker("network", "ls", "-q", "--filter", f"name={pattern}", check=False).stdout.split()


@pytest.fixture(scope="module")
def fake_source() -> Iterator[str]:
    """A disposable stand-in for production, carrying a synthetic `learnings`.

    The finalizer runs whatever the test did — that is the whole discipline this
    file is about, and a bench test that leaks a container would be arguing
    against itself.
    """
    name = f"w44-churn-src-{uuid.uuid4().hex[:8]}"
    _docker(
        "run",
        "-d",
        "--name",
        name,
        "--network",
        "none",
        "--tmpfs",
        "/var/lib/postgresql/data:rw,size=1g",
        "-e",
        "POSTGRES_HOST_AUTH_METHOD=trust",
        "-e",
        "PGDATA=/var/lib/postgresql/data",
        IMAGE,
    )
    try:
        for _ in range(60):
            if (
                _docker("exec", name, "pg_isready", "-U", "postgres", "-q", check=False).returncode
                == 0
            ):
                break
            time.sleep(1)
        # `brain`/`brain` is what the script's COPY connects as.
        _docker(
            "exec",
            name,
            "psql",
            "-U",
            "postgres",
            "-q",
            "-c",
            "create role brain superuser login;",
            "-c",
            "create database brain owner brain;",
        )
        _docker(
            "exec",
            name,
            "psql",
            "-U",
            "brain",
            "-d",
            "brain",
            "-q",
            "-v",
            "ON_ERROR_STOP=1",
            "-c",
            "create extension vector;",
            "-c",
            f"create table learnings (id uuid primary key default gen_random_uuid(), "
            f"embedding vector({DIM}));",
            "-c",
            f"insert into learnings (embedding) select "
            f"(select array_agg(random())::vector from generate_series(1, {DIM})) "
            f"from generate_series(1, {CORPUS_ROWS});",
        )
        yield name
    finally:
        _docker("rm", "-f", name, check=False)


@pytest.fixture
def bench_name() -> Iterator[str]:
    """A unique bench name, and the proof that nothing of it survives the test."""
    name = f"w44-churn-{uuid.uuid4().hex[:8]}"
    try:
        yield name
    finally:
        _docker("rm", "-f", name, check=False)
        _docker("network", "rm", f"{name}_default", check=False)
        assert _names(f"^{name}$") == [], f"{name} survived the test"
        assert _networks(f"^{name}_default$") == [], f"{name}_default survived the test"


def _run(
    bench: str, source: str, *, builds: int = 2, timeout: int = 600
) -> subprocess.CompletedProcess[str]:
    return subprocess.run(
        ["bash", str(SCRIPT)],
        cwd=ROOT,
        env={
            **os.environ,
            "CHURN_CONTAINER": bench,
            "PROD_CONTAINER": source,
            "BUILDS": str(builds),
            "SRC_TABLE": "learnings",
        },
        capture_output=True,
        text=True,
        timeout=timeout,
    )


# ── proof 1: the production container is refused, with no docker gesture ─────


def test_the_bench_refuses_to_be_named_after_its_own_source(fake_source: str) -> None:
    """`CHURN_CONTAINER == PROD_CONTAINER` must abort BEFORE any trap is armed.

    A trap armed before this guard would destroy at the very moment of refusal —
    which is why the script orders them that way, and why this asserts the
    witness survives rather than only reading the exit code.
    """
    result = _run(fake_source, fake_source)

    assert result.returncode == 2, result.stderr
    assert "conteneur de production" in result.stderr
    assert _names(f"^{fake_source}$"), (
        "the refusal destroyed the very container it refused to touch"
    )


# ── proof 3: a foreign container holding the name is refused, witness intact ──


def test_a_foreign_container_holding_the_bench_name_is_refused(
    fake_source: str, bench_name: str
) -> None:
    """Destroying by homonymy is the accident this guard closes."""
    _docker("run", "-d", "--name", bench_name, "--network", "none", IMAGE, "sleep", "600")

    result = _run(bench_name, fake_source)

    assert result.returncode == 2, result.stderr
    assert "étranger" in result.stderr
    assert _names(f"^{bench_name}$"), "the foreign container was destroyed — the guard did not hold"


# ── proof 4: the nominal run leaves nothing behind ───────────────────────────


def test_the_nominal_run_provisions_measures_and_tears_down(
    fake_source: str, bench_name: str
) -> None:
    started = time.monotonic()
    result = _run(bench_name, fake_source)
    elapsed = time.monotonic() - started

    assert result.returncode == 0, result.stderr[-2000:]
    assert "provisionnement du banc isolé" in result.stdout
    assert "SYNTHÈSE churn" in result.stdout
    assert _names(f"^{bench_name}$") == [], "the bench container outlived its run"
    assert _networks(f"^{bench_name}_default$") == [], "the bench network outlived its run"
    assert elapsed < 600, f"nominal run took {elapsed:.0f}s"


# ── proof 5: SIGINT mid-run tears down and exits 130 ─────────────────────────


def test_sigint_mid_run_tears_the_bench_down(fake_source: str, bench_name: str) -> None:
    """`restore_signals=True` is load-bearing: without it SIGINT starts ignored."""
    process = subprocess.Popen(
        ["bash", str(SCRIPT)],
        cwd=ROOT,
        env={
            **os.environ,
            "CHURN_CONTAINER": bench_name,
            "PROD_CONTAINER": fake_source,
            "BUILDS": "5",
            "SRC_TABLE": "learnings",
        },
        stdout=subprocess.PIPE,
        stderr=subprocess.PIPE,
        text=True,
        restore_signals=True,
        start_new_session=False,
    )
    for _ in range(120):
        if _names(f"^{bench_name}$"):
            break
        time.sleep(0.5)
    else:  # pragma: no cover - the bench never came up
        process.kill()
        pytest.fail("the bench container never appeared; nothing to interrupt")

    process.send_signal(signal.SIGINT)
    process.communicate(timeout=180)

    assert process.returncode == 130, f"expected 130, got {process.returncode}"
    assert _names(f"^{bench_name}$") == [], "SIGINT left the bench container behind"
    assert _networks(f"^{bench_name}_default$") == [], "SIGINT left the bench network behind"
