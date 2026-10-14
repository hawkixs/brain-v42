"""Require later-day agreement before applying a check-based state change."""

from collections.abc import Sequence
from dataclasses import dataclass
from datetime import date

from brain_v42.knowledge.decide import Layer


@dataclass(frozen=True)
class EvaluationRecord:
    observed_day: date
    entry_digest: str
    fingerprint: str
    complete: bool
    invalidated: bool
    layer: Layer


def confirmed(history: Sequence[EvaluationRecord], candidate: EvaluationRecord) -> bool:
    """Read one entry's history in append order (oldest first), including or excluding
    the candidate. Duplicate same-day runs cannot manufacture confirmation.

    Propagation has its own immediately applied layer and cannot confirm or reset
    check agreement. Incomplete records are ignored entirely. Under §2.8, an
    invalidated evaluation is void, as if it never happened: it neither counts
    toward confirmation nor resets agreement.
    """
    if candidate.layer in {"structural", "propagation"}:
        return True
    if not candidate.complete or candidate.invalidated:
        return False
    days = {candidate.observed_day}
    for record in reversed(history):
        if not record.complete or record.invalidated or record.layer == "propagation":
            continue
        if (
            record.entry_digest != candidate.entry_digest
            or record.fingerprint != candidate.fingerprint
        ):
            break
        days.add(record.observed_day)
    return len(days) >= 2
