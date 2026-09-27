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
import time
from collections.abc import Awaitable, Callable, Sequence
from contextlib import suppress
from dataclasses import dataclass, field
from datetime import date, timedelta
from pathlib import Path
from typing import TYPE_CHECKING, Final, Literal, Protocol
from uuid import UUID

from brain_v42.facts.model import Unreadable
from brain_v42.facts.sources import release_sha_from_path
from brain_v42.models.claim_verdict import ClaimVerificationError
from brain_v42.repositories.pg_claim_nightly import VerifyRunOwnershipLost

if TYPE_CHECKING:
    from brain_v42.facts.model import FactTarget, Measurement
    from brain_v42.repositories.pg_claim_verdicts import VerdictRow


class NightlyClaimLike(Protocol):
    """What the orchestrator reads from a selected claim (`pg_claim_nightly.NightlyClaim`).

    Read-only properties, not plain attributes: `NightlyClaim` is a frozen
    dataclass, and a plain Protocol attribute annotation requires a SETTABLE
    variable, which a frozen field is not.
    """

    @property
    def id(self) -> UUID: ...
    @property
    def project_key(self) -> str: ...
    @property
    def fact_name(self) -> str: ...
    @property
    def target(self) -> str: ...
    @property
    def definition_version(self) -> int: ...


class RegistryLike(Protocol):
    """The subset of `FactRegistry` the fail-closed check and dry mode need."""

    def names(self) -> tuple[str, ...]: ...
    def refusals(self) -> dict[str, str]: ...
    def disabled(self) -> dict[str, str]: ...
    def describe(self, name: str) -> object: ...
    def expected_identity(self, target: FactTarget) -> object | None: ...
    async def measure(self, name: str, *, max_age: timedelta | None = None) -> Measurement: ...


class VerificationOutcomeLike(Protocol):
    """Read-only properties: `VerificationOutcome` is a frozen dataclass too."""

    @property
    def row(self) -> VerdictRow: ...
    @property
    def replayed(self) -> bool: ...


class VerifyOutcomeCaller(Protocol):
    """The one method the orchestrator needs from `ClaimVerificationService`."""

    async def verify_outcome(
        self,
        claim_id: UUID,
        issuer_identity: str,
        issuer_kind: Literal["robot", "human"],
        idempotency_key: str,
        *,
        project_key: str | None = None,
    ) -> VerificationOutcomeLike: ...


class OwnershipLike(Protocol):
    """The one method the orchestrator needs from `VerifyRunOwnership`."""

    async def check(self) -> None: ...


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
# T1.7: fail closed before any verification, in both modes (spec §6.1).
# ---------------------------------------------------------------------------


@dataclass(frozen=True, slots=True)
class PreconditionFailure:
    """Why the step must abort before touching a single claim."""

    reason: str


def check_preconditions(
    registry: RegistryLike,
    eligible_facts: Sequence[str],
    *,
    definitions_registered: bool,
) -> PreconditionFailure | None:
    """Refuse to start rather than durably misrecord a deployment defect as a verdict.

    A fact absent from `eligible_facts` is never checked here: a refused or
    disabled fact that no eligible claim names must not fail the step.
    """
    if not definitions_registered:
        return PreconditionFailure("definitions_unregistered")
    eligible = set(eligible_facts)
    refused = sorted(set(registry.refusals()) & eligible)
    if refused:
        return PreconditionFailure(f"unverifiable_target: {', '.join(refused)}")
    disabled = sorted(set(registry.disabled()) & eligible)
    if disabled:
        return PreconditionFailure(f"definition_drift: {', '.join(disabled)}")
    return None


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


# ---------------------------------------------------------------------------
# T1.6: the wet orchestration loop -- outcomes, the spec §6.3 error table, the
# deadline, the ownership fence, and the spec §6.6 status/rc precedence.
# ---------------------------------------------------------------------------

#: Status precedence: fail > partial > timeout > done (spec §6.6). A run-lock
#: loss overrides all four: no run-row write is even attempted (rc 7).
STATUS_TO_RC: Final[dict[str, int]] = {
    "done": 0,
    "timeout": 3,
    "partial": 5,
    "fail": 1,
    "ownership_lost": 7,
}

#: Per-claim errors that make the run `partial` (spec §6.3): a real anomaly,
#: never hidden under a controlled deadline.
_PARTIAL_ERROR_CODES: Final = frozenset(
    {"claim_not_found", "invalid_emitted_at", "idempotency_conflict"}
)
#: Per-claim errors that cannot be a caller mistake in this in-process caller
#: (spec §2, §6.3): a programming error, so the whole run aborts.
_ABORT_ERROR_CODES: Final = frozenset({"invalid_argument", "unknown_actor"})


@dataclass(slots=True)
class NightlyReport:
    """The nightly step's own outcome. JSON-serialisable: claim ids only, never

    a statement or a measurement value (spec §7.1).
    """

    run_date: str
    run_id: int
    mode: Literal["dry", "wet"]
    selected: int = 0
    status: str = "done"
    holds: int = 0
    falsified: int = 0
    unreadable: dict[str, int] = field(default_factory=dict)
    replayed: int = 0
    skipped_budget: int = 0
    stopped_facts: list[str] = field(default_factory=list)
    skipped_deadline: int = 0
    skipped_release_mismatch: int = 0
    skipped_release_unknown: int = 0
    retired_mid_run: int = 0
    errors: dict[str, int] = field(default_factory=dict)
    #: Set only by `run_dry` (T1.7); `None` for a wet report.
    dry_claims: dict[str, object] | None = None
    dry_facts: dict[str, dict[str, object]] | None = None

    def as_dict(self) -> dict[str, object]:
        return {
            "run_date": self.run_date,
            "run_id": self.run_id,
            "mode": self.mode,
            "status": self.status,
            "rc": STATUS_TO_RC.get(self.status),
            "selected": self.selected,
            "holds": self.holds,
            "falsified": self.falsified,
            "unreadable": dict(self.unreadable),
            "replayed": self.replayed,
            "skipped_budget": self.skipped_budget,
            "stopped_facts": list(self.stopped_facts),
            "skipped_deadline": self.skipped_deadline,
            "skipped_release_mismatch": self.skipped_release_mismatch,
            "skipped_release_unknown": self.skipped_release_unknown,
            "retired_mid_run": self.retired_mid_run,
            "errors": dict(self.errors),
            "dry_claims": self.dry_claims,
            "dry_facts": self.dry_facts,
        }


class NightlyVerifier:
    """Bounded-concurrency wet loop over a pre-selected batch of claims.

    Every collaborator is injected: `service` (an owned-session
    `ClaimVerificationService`-shaped object exposing `verify_outcome`),
    `release_check` (a `ReleaseCheck`-shaped object), `ownership` (a
    `VerifyRunOwnership`-shaped object, `None` in dry mode: spec §3.4, dry
    mode takes no lock and never calls `verify_outcome`) and `clock` (a
    zero-argument monotonic-seconds callable).
    """

    def __init__(
        self,
        *,
        service: VerifyOutcomeCaller,
        release_check: ReleaseCheck,
        ownership: OwnershipLike | None = None,
        clock: Callable[[], float] = time.monotonic,
        max_concurrency: int = 4,
        deadline_seconds: float = 240.0,
    ) -> None:
        self._service = service
        self._release_check = release_check
        self._ownership = ownership
        self._clock = clock
        self._max_concurrency = max_concurrency
        self._deadline_seconds = deadline_seconds

    async def run(
        self,
        claims: Sequence[NightlyClaimLike],
        *,
        run_id: int,
        run_date: date,
        wet: bool,
    ) -> NightlyReport:
        report = NightlyReport(
            run_date=run_date.isoformat(),
            run_id=run_id,
            mode="wet" if wet else "dry",
            selected=len(claims),
        )
        if not claims:
            report.status = "done"
            return report

        start = self._clock()
        stopped_facts: set[str] = set()
        state = {"aborted": False, "ownership_lost": False, "next_index": 0}
        dispatch_lock = asyncio.Lock()
        lane_tasks: list[asyncio.Task[None]] = []
        deliberately_cancelled: set[asyncio.Task[None]] = set()

        def _cancel_other_lanes() -> None:
            """Stop siblings on our own abort/loss decision -- self-inflicted, not external."""
            current = asyncio.current_task()
            for other in lane_tasks:
                if other is not current:
                    deliberately_cancelled.add(other)
                    other.cancel()

        async def next_claim() -> NightlyClaimLike | None:
            async with dispatch_lock:
                if state["aborted"] or state["ownership_lost"]:
                    return None
                if self._clock() - start >= self._deadline_seconds:
                    return None
                index = state["next_index"]
                if index >= len(claims):
                    return None
                state["next_index"] = index + 1
                return claims[index]

        async def process(claim: NightlyClaimLike) -> None:
            if claim.fact_name in stopped_facts:
                report.skipped_budget += 1
                return
            if wet and self._ownership is not None:
                try:
                    await self._ownership.check()
                except VerifyRunOwnershipLost:
                    state["ownership_lost"] = True
                    _cancel_other_lanes()
                    return
            if claim.target == "live_release":
                decision = await self._release_check.decide()
                if decision.action == "skip_release_mismatch":
                    report.skipped_release_mismatch += 1
                    return
                if decision.action == "skip_release_unknown":
                    report.skipped_release_unknown += 1
                    return
            issuer = issuer_for(run_id)
            key = key_for(run_date)
            try:
                outcome = await self._service.verify_outcome(
                    claim.id, issuer, "robot", key, project_key=claim.project_key
                )
            except ClaimVerificationError as error:
                if error.code == "refresh_budget_exhausted":
                    stopped_facts.add(claim.fact_name)
                    report.skipped_budget += 1
                    return
                if error.code == "claim_retired":
                    report.retired_mid_run += 1
                    return
                report.errors[error.code] = report.errors.get(error.code, 0) + 1
                if error.code in _ABORT_ERROR_CODES:
                    state["aborted"] = True
                    _cancel_other_lanes()
                return
            if outcome.replayed:
                report.replayed += 1
            if outcome.row.verdict == "holds":
                report.holds += 1
            elif outcome.row.verdict == "falsified":
                report.falsified += 1
            elif outcome.row.verdict == "unreadable":
                reason = outcome.row.reason or "unknown"
                report.unreadable[reason] = report.unreadable.get(reason, 0) + 1

        async def lane() -> None:
            while True:
                claim = await next_claim()
                if claim is None:
                    return
                try:
                    await process(claim)
                except asyncio.CancelledError:
                    raise
                except Exception:
                    state["aborted"] = True
                    _cancel_other_lanes()
                    return

        concurrency = max(1, min(self._max_concurrency, len(claims)))
        lane_tasks[:] = [asyncio.create_task(lane()) for _ in range(concurrency)]
        results = await asyncio.gather(*lane_tasks, return_exceptions=True)

        # A lane's CancelledError is swallowed ONLY when WE caused it (a
        # deliberate sibling cancellation on our own abort/loss decision).
        # Anything else -- an outer cancellation reaching a lane, or a claim
        # whose own verification was cancelled independently -- must escape
        # `run()` uncaught (spec §6.5: "CancelledError is never caught").
        for task, result in zip(lane_tasks, results, strict=True):
            if isinstance(result, BaseException) and task not in deliberately_cancelled:
                raise result

        if not state["aborted"] and not state["ownership_lost"]:
            report.skipped_deadline = len(claims) - state["next_index"]
        report.stopped_facts = sorted(stopped_facts)

        if state["ownership_lost"]:
            report.status = "ownership_lost"
        elif state["aborted"]:
            report.status = "fail"
        elif any(report.errors.get(code, 0) for code in _PARTIAL_ERROR_CODES):
            report.status = "partial"
        elif report.skipped_deadline:
            report.status = "timeout"
        else:
            report.status = "done"
        return report

    async def run_dry(
        self,
        claims: Sequence[NightlyClaimLike],
        *,
        registry: RegistryLike,
        run_id: int,
        run_date: date,
    ) -> NightlyReport:
        """Classify and measure without ever calling `verify_outcome` (spec §3.4).

        No claim row lock, no run lock, no verdict. `release_check` gates a
        `live_release` claim exactly as the wet loop's `process()` does, so a
        dry rehearsal previews what the same claim would do wet -- checked
        BEFORE the historical-definition classification, matching `run()`'s
        own order (the release check gates whether `verify_outcome`, whose
        internals apply the historical check, is even reached).
        """
        report = NightlyReport(
            run_date=run_date.isoformat(),
            run_id=run_id,
            mode="dry",
            selected=len(claims),
        )
        names = set(registry.names())
        historical: list[dict[str, object]] = []
        release_skip = 0
        measurable: set[str] = set()

        for claim in claims:
            if claim.target == "live_release":
                decision = await self._release_check.decide()
                if decision.action != "verify":
                    release_skip += 1
                    continue
            if claim.fact_name not in names:
                historical.append(
                    {
                        "fact": claim.fact_name,
                        "stored_version": claim.definition_version,
                        "current_version": None,
                    }
                )
                continue
            descriptor = registry.describe(claim.fact_name)
            if (
                descriptor.definition_version != claim.definition_version  # type: ignore[attr-defined]
                or descriptor.target.value != claim.target  # type: ignore[attr-defined]
            ):
                historical.append(
                    {
                        "fact": claim.fact_name,
                        "stored_version": claim.definition_version,
                        "current_version": descriptor.definition_version,  # type: ignore[attr-defined]
                    }
                )
                continue
            measurable.add(claim.fact_name)

        dry_facts: dict[str, dict[str, object]] = {}
        for name in sorted(measurable):
            measurement = await registry.measure(name, max_age=None)
            if isinstance(measurement, Unreadable):
                dry_facts[name] = {
                    "status": "unreadable",
                    "error_code": measurement.error_code,
                    "identity_ok": None,
                }
            else:
                expected = registry.expected_identity(measurement.target)
                identity_ok = expected is not None and measurement.source == expected
                dry_facts[name] = {
                    "status": "measured",
                    "error_code": None,
                    "identity_ok": identity_ok,
                }

        report.dry_claims = {
            "historical_definition": historical,
            "release_skip": release_skip,
            "measurable": len(measurable),
        }
        report.dry_facts = dry_facts
        report.status = "done"
        return report
