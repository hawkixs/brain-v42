"""Per-rail proofs: what a live test measured, for which rail version (spec 0.5.0 §3.8.0).

Isolation and confinement are proven per rail, never assumed. A ``live`` test
(``tests/live/headless_agents/test_proofs_live.py``) measures them on the
installed rail and records the outcome here, in the operator's state
directory (plan decision P3)::

    <state>/proofs/<rail>.json
    {"rail": "codex", "version": "codex-cli 0.156.0",
     "isolation":   {"passed": true, "date": "2026-09-25"},
     "confinement": {"passed": true, "date": "2026-09-25"} | null}

A record holds for exactly the version it names: a rail upgrade needs a new
proof. The engine refuses a CLI rail without a passing isolation proof for
its installed version, and classifies a write role unconfined without a
passing confinement proof. HTTP providers need none (plan decision P4): they
run no local executor and load no operator configuration. A record that
cannot be read counts as none.
"""

from __future__ import annotations

import time
from dataclasses import dataclass
from pathlib import Path
from typing import Final

from .state import Unknown, publish, read_optional

CLI_RAILS: Final = ("claude", "codex", "agy", "opencode")


@dataclass(frozen=True)
class Proof:
    passed: bool
    date: str


@dataclass(frozen=True)
class ProofRecord:
    rail: str
    version: str | None
    isolation: Proof | None
    confinement: Proof | None


def proof_path(state: Path, rail: str) -> Path:
    return state / "proofs" / f"{rail}.json"


def _proof(value: object) -> Proof | None:
    if not isinstance(value, dict):
        return None
    passed, date = value.get("passed"), value.get("date")
    if not isinstance(passed, bool) or not isinstance(date, str):
        return None
    return Proof(passed=passed, date=date)


def read_proof(state: Path, rail: str) -> ProofRecord | None:
    """The record of ``rail``; ``None`` when there is none or it cannot be read."""
    try:
        document = read_optional(proof_path(state, rail), expect_id=("rail", rail))
    except Unknown:
        return None
    if document is None:
        return None
    version = document.get("version")
    return ProofRecord(
        rail=rail,
        version=version if isinstance(version, str) else None,
        isolation=_proof(document.get("isolation")),
        confinement=_proof(document.get("confinement")),
    )


def record_proof(
    state: Path,
    rail: str,
    *,
    version: str | None,
    isolation: bool | None = None,
    confinement: bool | None = None,
    today: str | None = None,
) -> ProofRecord:
    """Record what a live test measured. A new version replaces the whole record."""
    date = today or time.strftime("%Y-%m-%d", time.gmtime())
    previous = read_proof(state, rail)
    keep = previous if previous is not None and previous.version == version else None
    record = ProofRecord(
        rail=rail,
        version=version,
        isolation=Proof(isolation, date)
        if isolation is not None
        else (keep.isolation if keep else None),
        confinement=Proof(confinement, date)
        if confinement is not None
        else (keep.confinement if keep else None),
    )

    def as_dict(proof: Proof | None) -> dict[str, object] | None:
        return None if proof is None else {"passed": proof.passed, "date": proof.date}

    publish(
        proof_path(state, rail),
        {
            "rail": rail,
            "version": version,
            "isolation": as_dict(record.isolation),
            "confinement": as_dict(record.confinement),
        },
    )
    return record


def isolation_ok(state: Path, rail: str, version: str | None) -> bool:
    """May ``rail`` execute? HTTP providers always; a CLI rail with a passing proof."""
    if rail not in CLI_RAILS:
        return True
    record = read_proof(state, rail)
    return (
        record is not None
        and record.version == version
        and record.isolation is not None
        and record.isolation.passed
    )


def isolation_label(state: Path, rail: str, version: str | None) -> str:
    """How ``ha roles`` shows a rail's isolation."""
    if rail not in CLI_RAILS:
        return "not needed"
    record = read_proof(state, rail)
    if record is None or record.isolation is None:
        return "not proven"
    if record.version != version:
        return f"not proven for this version (proof of {record.version})"
    state_word = "isolated" if record.isolation.passed else "failed"
    return f"{state_word} ({record.isolation.date})"


__all__ = [
    "CLI_RAILS",
    "Proof",
    "ProofRecord",
    "isolation_label",
    "isolation_ok",
    "proof_path",
    "read_proof",
    "record_proof",
]
