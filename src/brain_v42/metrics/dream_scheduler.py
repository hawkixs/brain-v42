"""Measure scheduler arming independently of Dream's declared phase switches."""

from __future__ import annotations

import asyncio
from collections.abc import Awaitable, Callable
from contextlib import suppress
from typing import Any, Literal

import structlog

logger = structlog.get_logger(__name__)

DreamSchedulerMode = Literal["systemd_user", "none"]
SubprocessRunner = Callable[..., Awaitable[tuple[int, str]]]


async def _run_systemctl(*args: str) -> tuple[int, str]:
    """Reap cancelled probes so a timed-out measurement leaves no child behind."""
    process = await asyncio.create_subprocess_exec(
        *args, stdout=asyncio.subprocess.PIPE, stderr=asyncio.subprocess.DEVNULL
    )
    try:
        stdout, _ = await process.communicate()
    except asyncio.CancelledError:
        with suppress(ProcessLookupError):
            process.kill()
        await process.communicate()
        raise
    assert process.returncode is not None
    return process.returncode, stdout.decode("utf-8", errors="replace")


def _properties(output: str) -> dict[str, str | None]:
    """An empty systemd property is unavailable, not an invented value."""
    return {
        key: value or None
        for line in output.splitlines()
        for key, separator, value in [line.partition("=")]
        if separator
    }


async def read_dream_scheduler_state(
    mode: DreamSchedulerMode,
    runner: SubprocessRunner = _run_systemctl,
) -> dict[str, Any]:
    """Report declared absence or measured arming without depending on the DB."""
    result: dict[str, Any] = {
        "state": "not_measured",
        "source": "declared_none" if mode == "none" else "systemd_user",
        "timer": {"unit_file_state": None, "active_state": None, "next_elapse": None},
        "service": {"active_state": None},
        "error_type": None,
    }
    if mode == "none":
        result["state"] = "absent"
        return result

    try:
        # Bound the entire measurement, including both user-manager probes.
        async with asyncio.timeout(5):
            timer_code, timer_output = await runner(
                "systemctl",
                "--user",
                "show",
                "brain-v42-dream.timer",
                "--property=UnitFileState,ActiveState,NextElapseUSecRealtime,LoadState",
            )
            if timer_code != 0:
                result["error_type"] = f"systemctl_exit_{timer_code}"
                return result
            service_code, service_output = await runner(
                "systemctl", "--user", "show", "brain-v42-dream.service", "--property=ActiveState"
            )
            if service_code != 0:
                result["error_type"] = f"systemctl_exit_{service_code}"
                return result
        timer = _properties(timer_output)
        service = _properties(service_output)
        result["timer"] = {
            "unit_file_state": timer.get("UnitFileState"),
            "active_state": timer.get("ActiveState"),
            "next_elapse": timer.get("NextElapseUSecRealtime"),
        }
        result["service"] = {"active_state": service.get("ActiveState")}
        if timer.get("LoadState") == "not-found":
            result["state"] = "absent"
        elif timer.get("UnitFileState") == "enabled" and timer.get("ActiveState") == "active":
            result["state"] = "armed"
        else:
            result["state"] = "suspended"
    except Exception as exc:
        result["error_type"] = type(exc).__name__
        logger.warning("metrics.dream_scheduler.not_measured", error_type=result["error_type"])
    return result
