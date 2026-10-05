"""Boot-resilience contracts: long-running units wait for PostgreSQL and back off.

These are systemd --user units: they cannot order after docker.service (a system unit), so
the wait is a bounded ExecStartPre and the restart policy must never latch at the start limit.
"""

from __future__ import annotations

import re
from pathlib import Path

import pytest
from scripts.wait_for_postgres import MAX_WAIT_SECONDS

from tests.unit.deploy.test_systemd_sandbox_profiles import _directives, _section_directives

LONG_RUNNING = {
    "brain-metrics.service.tmpl": ("BRAIN_POSTGRES_URL", "POSTGRES_URL"),
    "brain-mcp-http.service.tmpl": ("BRAIN_POSTGRES_URL", "POSTGRES_URL"),
    "brain-v42-automation.service.tmpl": ("BRAIN_POSTGRES_URL", "POSTGRES_URL"),
    "brain-v42-delivery-observer.service.tmpl": ("BRAIN_DELIVERY_POSTGRES_URL", "POSTGRES_URL"),
}
TEMPLATE_DIR = Path(__file__).resolve().parents[3] / "deploy" / "systemd"
BACKOFF_CEILING_SECONDS = 60


def _values(unit: str, key: str) -> list[str]:
    return [value for name, value in _directives(unit) if name == key]


def _single(unit: str, key: str, section: str = "Service") -> str:
    values = [value for name, value in _section_directives(unit, section) if name == key]
    assert len(values) == 1, f"{unit} must declare {key} exactly once: {values}"
    return values[0]


def _wait_command(unit: str) -> str:
    waits = [v for v in _values(unit, "ExecStartPre") if "scripts/wait_for_postgres.py" in v]
    assert len(waits) == 1, f"{unit} must run the Postgres readiness gate exactly once"
    return waits[0]


@pytest.mark.parametrize("unit", LONG_RUNNING)
def test_postgres_readiness_gate_runs_after_the_config_preflights(unit: str) -> None:
    pre = _values(unit, "ExecStartPre")

    assert "scripts/wait_for_postgres.py" in pre[-1]


@pytest.mark.parametrize(("unit", "variables"), LONG_RUNNING.items())
def test_readiness_gate_reads_the_url_from_the_environment_not_the_command_line(
    unit: str, variables: tuple[str, ...]
) -> None:
    command = _wait_command(unit)

    assert re.findall(r"--url-env (\S+)", command) == list(variables)
    assert "://" not in command
    assert "$" not in command


@pytest.mark.parametrize("unit", LONG_RUNNING)
def test_readiness_wait_is_bounded_and_the_unit_timeout_encloses_it(unit: str) -> None:
    wait = int(re.search(r"--timeout (\d+)", _wait_command(unit)).group(1))  # type: ignore[union-attr]
    start_timeout = int(_single(unit, "TimeoutStartSec").removesuffix("s"))

    assert 0 < wait <= MAX_WAIT_SECONDS
    assert wait < start_timeout <= MAX_WAIT_SECONDS + 30


@pytest.mark.parametrize("unit", LONG_RUNNING)
def test_restarts_back_off_up_to_a_ceiling(unit: str) -> None:
    initial = int(_single(unit, "RestartSec").removesuffix("s"))
    steps = int(_single(unit, "RestartSteps"))
    ceiling = int(_single(unit, "RestartMaxDelaySec").removesuffix("s"))

    assert steps >= 4
    assert initial < ceiling <= BACKOFF_CEILING_SECONDS


@pytest.mark.parametrize("unit", LONG_RUNNING)
def test_start_limit_cannot_latch_a_long_running_unit(unit: str) -> None:
    assert _single(unit, "StartLimitIntervalSec", "Unit") == "0"
    assert not [k for k, _ in _section_directives(unit, "Unit") if k == "StartLimitBurst"]


def test_oneshot_and_timer_driven_units_keep_their_semantics() -> None:
    templates = sorted(TEMPLATE_DIR.glob("*.service.tmpl"))
    units = [path.name for path in templates if _single(path.name, "Type") == "oneshot"]

    assert units
    for unit in units:
        assert _single(unit, "Type") == "oneshot", unit
        keys = {key for key, _ in _directives(unit)}
        assert keys.isdisjoint(
            {"Restart", "RestartSteps", "RestartMaxDelaySec", "StartLimitIntervalSec"}
        ), unit
        assert not any("wait_for_postgres" in v for v in _values(unit, "ExecStartPre")), unit
