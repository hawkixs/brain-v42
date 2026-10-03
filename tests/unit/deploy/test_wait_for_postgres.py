"""Bounded PostgreSQL readiness gate run as ExecStartPre by the long-running units."""

import subprocess
import sys
from pathlib import Path

import pytest
from scripts import wait_for_postgres as readiness
from scripts.wait_for_postgres import (
    MAX_WAIT_SECONDS,
    resolve_dsn,
    wait_for_postgres,
)

SCRIPT = Path(__file__).resolve().parents[3] / "scripts" / "wait_for_postgres.py"
PASSWORD = "wait-for-postgres-canary-password"


class _Clock:
    """Deterministic clock: sleeping is the only thing that advances time."""

    def __init__(self) -> None:
        self.now = 0.0
        self.sleeps: list[float] = []

    def monotonic(self) -> float:
        return self.now

    def sleep(self, seconds: float) -> None:
        self.sleeps.append(seconds)
        self.now += seconds


def test_dsn_comes_from_the_first_named_variable_that_is_set_and_drops_the_driver() -> None:
    dsn = resolve_dsn(
        ["BRAIN_POSTGRES_URL", "POSTGRES_URL"],
        {"POSTGRES_URL": f"postgresql+asyncpg://brain:{PASSWORD}@127.0.0.1:5433/brain"},
    )

    assert dsn == f"postgresql://brain:{PASSWORD}@127.0.0.1:5433/brain"


def test_the_earlier_variable_wins_when_both_are_set() -> None:
    dsn = resolve_dsn(
        ["BRAIN_POSTGRES_URL", "POSTGRES_URL"],
        {
            "BRAIN_POSTGRES_URL": "postgresql+asyncpg://a@h1/db",
            "POSTGRES_URL": "postgresql+asyncpg://b@h2/db",
        },
    )

    assert dsn == "postgresql://a@h1/db"


def test_no_variable_set_is_a_configuration_error() -> None:
    with pytest.raises(ValueError, match="BRAIN_POSTGRES_URL, POSTGRES_URL"):
        resolve_dsn(["BRAIN_POSTGRES_URL", "POSTGRES_URL"], {"POSTGRES_URL": "  "})


def test_unexpected_url_parser_errors_are_not_reclassified(monkeypatch: pytest.MonkeyPatch) -> None:
    def fail_unexpectedly(_value: str) -> None:
        raise RuntimeError("unexpected parser failure")

    monkeypatch.setattr(readiness, "make_url", fail_unexpectedly)

    with pytest.raises(RuntimeError, match="unexpected parser failure"):
        resolve_dsn(["POSTGRES_URL"], {"POSTGRES_URL": "postgresql://host/db"})


def test_returns_as_soon_as_postgres_answers() -> None:
    clock = _Clock()
    attempts: list[str] = []

    ready = wait_for_postgres(
        "postgresql://h/db",
        timeout=90,
        interval=2,
        probe=lambda dsn: attempts.append(dsn),
        monotonic=clock.monotonic,
        sleep=clock.sleep,
    )

    assert ready is True
    assert attempts == ["postgresql://h/db"]
    assert clock.sleeps == []


def test_retries_until_postgres_answers() -> None:
    clock = _Clock()
    calls = 0

    def probe(_dsn: str) -> None:
        nonlocal calls
        calls += 1
        if calls < 4:
            raise ConnectionRefusedError

    ready = wait_for_postgres(
        "postgresql://h/db",
        timeout=90,
        interval=2,
        probe=probe,
        monotonic=clock.monotonic,
        sleep=clock.sleep,
    )

    assert ready is True
    assert calls == 4
    assert clock.sleeps == [2, 2, 2]


def test_gives_up_at_the_deadline_and_never_sleeps_past_it() -> None:
    clock = _Clock()

    def probe(_dsn: str) -> None:
        raise ConnectionRefusedError

    ready = wait_for_postgres(
        "postgresql://h/db",
        timeout=10,
        interval=4,
        probe=probe,
        monotonic=clock.monotonic,
        sleep=clock.sleep,
    )

    assert ready is False
    assert clock.now == 10
    assert clock.sleeps == [4, 4, 2]


def test_wait_above_the_hard_cap_is_refused_before_any_attempt() -> None:
    completed = subprocess.run(
        [sys.executable, str(SCRIPT), "--url-env", "X", "--timeout", str(MAX_WAIT_SECONDS + 1)],
        capture_output=True,
        text=True,
        check=False,
        timeout=20,
        env={"X": "postgresql+asyncpg://u@127.0.0.1:1/db"},
    )

    assert completed.returncode == 2
    assert str(MAX_WAIT_SECONDS) in completed.stderr


def test_unreachable_postgres_fails_within_the_bound_without_leaking_the_password() -> None:
    completed = subprocess.run(
        [sys.executable, str(SCRIPT), "--url-env", "X", "--timeout", "1", "--interval", "0.2"],
        capture_output=True,
        text=True,
        check=False,
        timeout=20,
        env={"X": f"postgresql+asyncpg://brain:{PASSWORD}@127.0.0.1:1/brain"},
    )

    assert completed.returncode == 1
    assert PASSWORD not in completed.stdout + completed.stderr
    assert "not ready" in completed.stderr


def test_probe_failure_does_not_leak_url_or_password() -> None:
    url = f"postgresql+asyncpg://u:{PASSWORD}@127.0.0.1:1/db"
    completed = subprocess.run(
        [sys.executable, str(SCRIPT), "--url-env", "X", "--timeout", "1", "--interval", "0.2"],
        capture_output=True,
        text=True,
        check=False,
        timeout=20,
        env={"X": url},
    )

    assert completed.returncode == 1
    assert PASSWORD not in completed.stdout + completed.stderr
    assert url not in completed.stdout + completed.stderr


def test_missing_configuration_fails_fast_with_a_distinct_exit_code() -> None:
    completed = subprocess.run(
        [sys.executable, str(SCRIPT), "--url-env", "X"],
        capture_output=True,
        text=True,
        check=False,
        timeout=20,
        env={},
    )

    assert completed.returncode == 2
    assert "X" in completed.stderr


@pytest.mark.parametrize(
    "url",
    [
        f"postgresql+asyncpg://u:{PASSWORD}@host:abc{PASSWORD}/db",
        f"postgresql+asyncpg://u:{PASSWORD}@host:999999/db",
    ],
)
def test_malformed_configuration_does_not_leak_url_or_password(url: str) -> None:
    completed = subprocess.run(
        [sys.executable, str(SCRIPT), "--url-env", "X", "--timeout", "1"],
        capture_output=True,
        text=True,
        check=False,
        timeout=20,
        env={"X": url},
    )

    assert completed.returncode == 2
    assert PASSWORD not in completed.stdout + completed.stderr
    assert url not in completed.stdout + completed.stderr
    assert "X" in completed.stderr
