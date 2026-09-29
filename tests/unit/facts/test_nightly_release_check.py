"""`ReleaseCheck` decides whether a `live_release` claim may be verified tonight.

Q6 (spec §6.2): verify only when the Dream CLI and the running `brain-mcp-http`
resolve to the SAME immutable release SHA; otherwise skip and say why. Every
cause of "unknown" must be a skip, never a raise -- a release check is not
allowed to abort the whole nightly run over one bad reading.

NOTE for reviewers: the spec's own "First" step for this task
(`systemd-run --user ...` proving `/proc/<MainPID>/exe` is readable from
inside the Dream unit's sandbox) could not be run here: this container has no
user systemd session and no running `brain-mcp-http` service. Only the
dependency-injected design below is tested; the REAL default
`systemctl`/`/proc` implementation is exercised by neither this file nor any
other in this PR.
"""

from __future__ import annotations

import asyncio
from pathlib import Path

import pytest

from brain_v42.facts.nightly import ReleaseCheck

_SHA_A = "a" * 40
_SHA_B = "b" * 40


def _cli_path(sha: str) -> Path:
    return Path(f"/opt/brain-v42/releases/{sha}/venv/lib/python3.12/brain_v42/__init__.py")


def _exe_path(sha: str) -> Path:
    return Path(f"/opt/brain-v42/releases/{sha}/venv/bin/python")


def _runner(active_state: str, main_pid: str):
    async def runner() -> tuple[str, str]:
        return active_state, main_pid

    return runner


def _sequenced_runner(*results: tuple[str, str]):
    iterator = iter(results)

    async def runner() -> tuple[str, str]:
        return next(iterator)

    return runner


async def test_equal_shas_verify() -> None:
    check = ReleaseCheck(
        cli_release_path=_cli_path(_SHA_A),
        systemctl_runner=_runner("active", "4242"),
        proc_reader=lambda pid: str(_exe_path(_SHA_A)),
    )

    decision = await check.decide()

    assert decision.action == "verify"
    assert decision.cli_sha == _SHA_A
    assert decision.server_sha == _SHA_A


async def test_different_shas_skip_as_mismatch_with_both_shas_reported() -> None:
    check = ReleaseCheck(
        cli_release_path=_cli_path(_SHA_A),
        systemctl_runner=_runner("active", "4242"),
        proc_reader=lambda pid: str(_exe_path(_SHA_B)),
    )

    decision = await check.decide()

    assert decision.action == "skip_release_mismatch"
    assert decision.cli_sha == _SHA_A
    assert decision.server_sha == _SHA_B


async def test_systemctl_nonzero_exit_skips_unknown() -> None:
    async def runner() -> tuple[str, str]:
        raise RuntimeError("systemctl exited 3")

    check = ReleaseCheck(
        cli_release_path=_cli_path(_SHA_A), systemctl_runner=runner, proc_reader=lambda pid: ""
    )

    decision = await check.decide()

    assert decision.action == "skip_release_unknown"
    assert decision.unknown_reason == "systemctl_failed"
    assert decision.cli_sha == _SHA_A
    assert decision.server_sha is None


async def test_systemctl_timeout_skips_unknown() -> None:
    async def runner() -> tuple[str, str]:
        raise TimeoutError()

    check = ReleaseCheck(
        cli_release_path=_cli_path(_SHA_A), systemctl_runner=runner, proc_reader=lambda pid: ""
    )

    decision = await check.decide()

    assert decision.action == "skip_release_unknown"
    assert decision.unknown_reason == "systemctl_timeout"


async def test_inactive_unit_skips_unknown() -> None:
    check = ReleaseCheck(
        cli_release_path=_cli_path(_SHA_A),
        systemctl_runner=_runner("inactive", "4242"),
        proc_reader=lambda pid: "",
    )

    decision = await check.decide()

    assert decision.action == "skip_release_unknown"
    assert decision.unknown_reason == "unit_inactive"


async def test_zero_main_pid_skips_unknown() -> None:
    check = ReleaseCheck(
        cli_release_path=_cli_path(_SHA_A),
        systemctl_runner=_runner("active", "0"),
        proc_reader=lambda pid: "",
    )

    decision = await check.decide()

    assert decision.action == "skip_release_unknown"
    assert decision.unknown_reason == "main_pid_zero"


async def test_non_integer_main_pid_skips_unknown() -> None:
    check = ReleaseCheck(
        cli_release_path=_cli_path(_SHA_A),
        systemctl_runner=_runner("active", "not-a-pid"),
        proc_reader=lambda pid: "",
    )

    decision = await check.decide()

    assert decision.action == "skip_release_unknown"
    assert decision.unknown_reason == "main_pid_not_integer"


async def test_unreadable_proc_skips_unknown() -> None:
    def proc_reader(pid: int) -> str:
        raise OSError("no such process")

    check = ReleaseCheck(
        cli_release_path=_cli_path(_SHA_A),
        systemctl_runner=_runner("active", "4242"),
        proc_reader=proc_reader,
    )

    decision = await check.decide()

    assert decision.action == "skip_release_unknown"
    assert decision.unknown_reason == "proc_exe_unreadable"


async def test_exe_path_without_a_release_segment_skips_unknown() -> None:
    check = ReleaseCheck(
        cli_release_path=_cli_path(_SHA_A),
        systemctl_runner=_runner("active", "4242"),
        proc_reader=lambda pid: "/usr/bin/python3.12",
    )

    decision = await check.decide()

    assert decision.action == "skip_release_unknown"
    assert decision.unknown_reason == "server_exe_no_release_segment"


async def test_cli_path_without_a_release_segment_skips_unknown_before_any_call() -> None:
    calls = 0

    async def runner() -> tuple[str, str]:
        nonlocal calls
        calls += 1
        return "active", "4242"

    check = ReleaseCheck(
        cli_release_path=Path("/usr/lib/python3.12/site-packages/brain_v42/__init__.py"),
        systemctl_runner=runner,
        proc_reader=lambda pid: "",
    )

    decision = await check.decide()

    assert decision.action == "skip_release_unknown"
    assert decision.unknown_reason == "cli_path_no_release_segment"
    assert calls == 0


async def test_the_check_is_evaluated_fresh_each_call_so_a_change_is_observed_on_the_second() -> (
    None
):
    check = ReleaseCheck(
        cli_release_path=_cli_path(_SHA_A),
        systemctl_runner=_sequenced_runner(("active", "1"), ("active", "2")),
        proc_reader=lambda pid: str(_exe_path(_SHA_A if pid == 1 else _SHA_B)),
    )

    first = await check.decide()
    second = await check.decide()

    assert first.action == "verify"
    assert second.action == "skip_release_mismatch"


async def test_cancellation_propagates_instead_of_being_treated_as_unknown() -> None:
    async def runner() -> tuple[str, str]:
        raise asyncio.CancelledError()

    check = ReleaseCheck(
        cli_release_path=_cli_path(_SHA_A), systemctl_runner=runner, proc_reader=lambda pid: ""
    )

    with pytest.raises(asyncio.CancelledError):
        await check.decide()
