"""The nightly claim-verification step (ADR 27 lot C): issuer and key formats.

Both formats are module constants so the orchestrator, the `claim_verify` CLI
and the `claims_verification_last_night` probe (PR 3) agree on the exact
literal a verdict's `issuer_identity`/`idempotency_key` carries -- the fact's
exact-match query (spec §7.2) depends on this being the ONE place either
format is built.
"""

from __future__ import annotations

import asyncio
import os
from collections.abc import Awaitable, Callable
from contextlib import suppress
from dataclasses import dataclass
from datetime import date
from pathlib import Path
from typing import Final, Literal

from brain_v42.facts.sources import release_sha_from_path

#: No product path outside this step mints this prefix (spec §2): every other
#: MCP issuer is `mcp:<actor>` (`mcp/tools/claim_tools.py`).
ISSUER_PREFIX: Final = "dream:verify:"

#: Constant per run date: every verdict the step writes for one run carries
#: exactly this key, derived from that run's OWN `run_date` (spec §5.2).
KEY_PREFIX: Final = "dream-verify:v1:"


def issuer_for(run_id: int) -> str:
    """The declared issuer for verdicts written by the nightly run `run_id`."""
    return f"{ISSUER_PREFIX}{run_id}"


def key_for(run_date: date) -> str:
    """The idempotency key shared by every verdict one run writes."""
    return f"{KEY_PREFIX}{run_date.isoformat()}"


# ---------------------------------------------------------------------------
# T1.5: verify `live_release` claims only on the server's OWN running release
# (spec §6.2, Q6). `ReleaseCheck` is claim-agnostic -- the orchestrator (T1.6)
# is the one that calls it only for a claim whose `target == "live_release"`.
# ---------------------------------------------------------------------------

#: The evidence `scripts/check_delivery_deployment.py:_SYSTEMD_TIMEOUT_SECONDS`
#: already uses for a `systemctl --user` round trip.
_SYSTEMCTL_TIMEOUT_SECONDS: Final = 5

#: `(ActiveState, MainPID)`, both the RAW strings `systemctl --user show`
#: prints -- parsing (a non-integer MainPID included) is `ReleaseCheck`'s job,
#: not the runner's, so every unknown cause is one of ITS branches.
SystemctlRunner = Callable[[], Awaitable[tuple[str, str]]]
#: Reads `/proc/<pid>/exe`'s symlink target; raises `OSError` if it cannot.
ProcReader = Callable[[int], str]

ReleaseAction = Literal["verify", "skip_release_mismatch", "skip_release_unknown"]


@dataclass(frozen=True, slots=True)
class ReleaseDecision:
    """What the nightly step should do with one `live_release` claim, and why."""

    action: ReleaseAction
    cli_sha: str | None
    server_sha: str | None
    unknown_reason: str | None


async def _default_systemctl_runner() -> tuple[str, str]:
    proc = await asyncio.create_subprocess_exec(
        "systemctl",
        "--user",
        "show",
        "brain-mcp-http.service",
        "-p",
        "ActiveState",
        "-p",
        "MainPID",
        stdout=asyncio.subprocess.PIPE,
        stderr=asyncio.subprocess.DEVNULL,
    )
    try:
        async with asyncio.timeout(_SYSTEMCTL_TIMEOUT_SECONDS):
            stdout, _ = await proc.communicate()
    except TimeoutError:
        with suppress(ProcessLookupError):
            proc.kill()
        await proc.wait()
        raise
    if proc.returncode != 0:
        raise RuntimeError(f"systemctl exited {proc.returncode}")
    values = dict(
        line.split("=", 1)
        for line in stdout.decode("utf-8", errors="replace").splitlines()
        if "=" in line
    )
    return values.get("ActiveState", ""), values.get("MainPID", "")


def _default_proc_reader(pid: int) -> str:
    return os.readlink(f"/proc/{pid}/exe")


class ReleaseCheck:
    """Compare the Dream CLI's own release against the running `brain-mcp-http`'s.

    Evaluated fresh on every call (no caching): the spec requires a server
    restart mid-invocation to be observed on the NEXT claim, not carried over
    from an earlier one.
    """

    def __init__(
        self,
        *,
        cli_release_path: Path,
        systemctl_runner: SystemctlRunner | None = None,
        proc_reader: ProcReader | None = None,
    ) -> None:
        self._cli_release_path = cli_release_path
        self._systemctl_runner = systemctl_runner or _default_systemctl_runner
        self._proc_reader = proc_reader or _default_proc_reader

    async def decide(self) -> ReleaseDecision:
        try:
            cli_sha = release_sha_from_path(self._cli_release_path)
        except ValueError:
            return ReleaseDecision(
                "skip_release_unknown", None, None, "cli_path_no_release_segment"
            )

        try:
            active_state, main_pid_raw = await self._systemctl_runner()
        except asyncio.CancelledError:
            raise
        except TimeoutError:
            return ReleaseDecision("skip_release_unknown", cli_sha, None, "systemctl_timeout")
        except Exception:
            return ReleaseDecision("skip_release_unknown", cli_sha, None, "systemctl_failed")

        if active_state != "active":
            return ReleaseDecision("skip_release_unknown", cli_sha, None, "unit_inactive")
        try:
            main_pid = int(main_pid_raw)
        except ValueError:
            return ReleaseDecision("skip_release_unknown", cli_sha, None, "main_pid_not_integer")
        if main_pid <= 0:
            return ReleaseDecision("skip_release_unknown", cli_sha, None, "main_pid_zero")

        try:
            exe = self._proc_reader(main_pid)
        except OSError:
            return ReleaseDecision("skip_release_unknown", cli_sha, None, "proc_exe_unreadable")

        try:
            server_sha = release_sha_from_path(Path(exe))
        except ValueError:
            return ReleaseDecision(
                "skip_release_unknown", cli_sha, None, "server_exe_no_release_segment"
            )

        if server_sha == cli_sha:
            return ReleaseDecision("verify", cli_sha, server_sha, None)
        return ReleaseDecision("skip_release_mismatch", cli_sha, server_sha, None)
