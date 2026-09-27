"""Per-rail proof status, for a person, before a run fails (spec 0.5.2, lot 2; spec G3, Q2).

``ha providers`` used to say only whether a rail's executable was found. A CLI that
updated itself overnight -- claude 2.1.282 to 2.1.283, with the isolation proof still
naming 2.1.282 -- then refused every run on that rail with nothing having said so
beforehand: the operator only learned it from a run failing. This module turns
:mod:`headless_agents.proofs`' pass/fail gate into a status a person can read ahead of a
run: which of five things (``passed``, ``failed``, ``missing``, ``stale``, ``unreadable``)
happened to a rail's isolation or confinement record, and the resulting mode.

**The engine stays the sole authority.** :data:`RailState.mode` is computed only from
:func:`headless_agents.proofs.isolation_ok` and :func:`headless_agents.proofs.confinement`
-- the exact functions the engine calls before a run -- never derived from the status
below; a display bug here can misreport a status but can never let a run onto an
unproven rail or hide one that is proven. The status itself is built by reading the raw
record one level below :func:`headless_agents.proofs.read_proof`, which already collapses
"no record" and "a record that does not parse" into the same ``None`` -- by design, for
the engine's fail-closed gate, which treats both alike. This module tells them apart as
``missing`` and ``unreadable`` because an operator fixes those two differently: a missing
proof is recorded with ``ha prove``; an unreadable one names a bug or a hand-edited file.

``stale`` always names what the proof was recorded for: another version of the rail, or
-- for isolation only, at the SAME rail version -- the installed package's own isolation
source moving under it (:func:`headless_agents.proofs.isolation_fingerprint`), since a
headless-agents upgrade can change how a rail is isolated without the rail's own
``--version`` string changing at all.

A rail's isolation and confinement proofs share ONE ``version`` field in the record
(:mod:`headless_agents.proofs`' own schema): they can never be independently stale by
version within a single record -- a version mismatch makes both stale at once (if they
have any recorded content; a kind with none stays ``missing`` regardless, since that is
checked first).
"""

from __future__ import annotations

from collections.abc import Sequence
from dataclasses import dataclass
from pathlib import Path
from typing import Final, Literal

from . import proofs
from .state import Unknown, read_optional

Status = Literal["passed", "failed", "missing", "stale", "unreadable"]
Mode = Literal["refused", "writes serialised", "parallel"]

#: Rails whose confinement can never be proven: a live probe has no way to tie a
#: rejected write to a path (operator decision Q91=b), so the displayed reason says so
#: instead of an invitation to re-prove a thing no proof can settle either way.
UNPROVABLE_CONFINEMENT: Final[dict[str, str]] = {
    "claude": "claude's tool log names no path for a rejected call (Q91=b)",
}


@dataclass(frozen=True)
class ProofStatus:
    status: Status
    date: str | None
    recorded_version: str | None
    reason: str


@dataclass(frozen=True)
class RailState:
    rail: str
    version: str | None
    isolation: ProofStatus
    confinement: ProofStatus
    mode: Mode
    reprove: str | None


def _missing(reason: str) -> ProofStatus:
    return ProofStatus(status="missing", date=None, recorded_version=None, reason=reason)


def _unreadable(reason: str) -> ProofStatus:
    return ProofStatus(status="unreadable", date=None, recorded_version=None, reason=reason)


def _no_record_reason(rail: str, kind: Literal["isolation", "confinement"]) -> str:
    """Why ``rail``'s ``kind`` has no record at all -- the ONLY case "unprovable" ever
    names, reserved for a rail with no usable proof either way (review round, PR #241,
    item 1): a rail actually holding a confinement record is shown exactly like any
    other rail, whatever its status turns out to be."""
    if kind == "confinement" and rail in UNPROVABLE_CONFINEMENT:
        return f"unprovable on {rail} ({UNPROVABLE_CONFINEMENT[rail]})"
    return "no proof recorded"


def proof_status(
    state: Path, rail: str, kind: Literal["isolation", "confinement"], version: str | None
) -> ProofStatus:
    """The status of ``rail``'s ``kind`` proof against the installed ``version``."""
    try:
        document = read_optional(proofs.proof_path(state, rail), expect_id=("rail", rail))
    except Unknown:
        return _unreadable(f"{rail}.json does not name {rail!r} or does not parse")
    if document is None:
        return _missing(_no_record_reason(rail, kind))
    raw = document.get(kind)
    if raw is None:
        return _missing(_no_record_reason(rail, kind))
    if not isinstance(raw, dict):
        return _unreadable(f"{kind}: recorded value is not a table")
    passed, date = raw.get("passed"), raw.get("date")
    if not isinstance(passed, bool) or not isinstance(date, str):
        return _unreadable(f"{kind}: recorded value has no boolean passed and string date")

    raw_version = document.get("version")
    recorded_version = raw_version if isinstance(raw_version, str) else None
    if recorded_version != version:
        return ProofStatus(
            status="stale",
            date=date,
            recorded_version=recorded_version,
            reason=f"recorded for {recorded_version}",
        )
    if passed is False:
        return ProofStatus(
            status="failed",
            date=date,
            recorded_version=recorded_version,
            reason=f"failed ({date})",
        )
    if kind == "isolation":
        fingerprint = raw.get("fingerprint")
        if isinstance(fingerprint, str) and fingerprint != proofs.isolation_fingerprint(rail):
            return ProofStatus(
                status="stale",
                date=date,
                recorded_version=recorded_version,
                reason="isolation source changed since the proof (headless-agents upgrade)",
            )
    return ProofStatus(
        status="passed", date=date, recorded_version=recorded_version, reason=f"passed ({date})"
    )


def reprove_command(rail: str, kinds: Sequence[str]) -> str:
    """The one command that re-proves ``rail`` for ``kinds``: ``ha prove`` (lot 4a).

    A single kind names its flag; more than one names none, since ``ha prove RAIL``
    proves every kind it can for the rail in one pass. The installed ``ha`` records the
    proof: no repository checkout, no pytest.
    """
    kinds = list(kinds)
    return f"ha prove {rail} --{kinds[0]}" if len(kinds) == 1 else f"ha prove {rail}"


def rail_state(state: Path, rail: str, version: str | None) -> RailState:
    """``rail``'s full proof picture: both kinds' status, and the mode they leave it in."""
    isolation = proof_status(state, rail, "isolation", version)
    confinement_status = proof_status(state, rail, "confinement", version)

    needs = [
        kind
        for kind, status in (("isolation", isolation), ("confinement", confinement_status))
        if status.status != "passed"
    ]
    if rail in UNPROVABLE_CONFINEMENT:
        # A live probe cannot prove confinement here either way (Q91=b): offering a
        # reprove command for it would spend tokens on a probe that stays inconclusive.
        needs = [kind for kind in needs if kind != "confinement"]

    if not proofs.isolation_ok(state, rail, version):
        mode: Mode = "refused"
    elif proofs.confinement(state, rail, version)[0] == "confined":
        mode = "parallel"
    else:
        mode = "writes serialised"

    return RailState(
        rail=rail,
        version=version,
        isolation=isolation,
        confinement=confinement_status,
        mode=mode,
        reprove=reprove_command(rail, needs) if needs else None,
    )


__all__ = [
    "Mode",
    "ProofStatus",
    "RailState",
    "Status",
    "UNPROVABLE_CONFINEMENT",
    "proof_status",
    "rail_state",
    "reprove_command",
]
