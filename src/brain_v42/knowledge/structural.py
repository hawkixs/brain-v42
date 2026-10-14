"""Turn supplied structural rows into evidence without querying successors."""

import hashlib
import json
from collections.abc import Mapping
from datetime import date, datetime

from brain_v42.knowledge.decide import Observation, StructuralRule
from brain_v42.knowledge.ranks import RANKS, Polarity, ValidFromPrecision


def structural_observations(table: str, row: Mapping[str, object]) -> list[Observation]:
    """Keep source acquisition outside the decision layer.

    The pipeline row contract supplies ``successor_exists`` as a bool.
    """
    observations = []

    def emit(rule: StructuralRule, value: object, at: object) -> None:
        day = at.date() if isinstance(at, datetime) else at if isinstance(at, date) else None
        kind = "decay" if rule == "decay_archived" else "structure"
        observed = value if isinstance(value, (str, int)) else str(value)
        source_ref = f"{table}:{row.get('id', '')}:{rule}:{observed}"
        payload = json.dumps([source_ref, day.isoformat() if day else None], separators=(",", ":"))
        observations.append(
            Observation(
                id=hashlib.sha256(payload.encode()).hexdigest(),
                assertion_key=None,
                polarity=Polarity.CONTRADICTS if kind == "decay" else Polarity.ENDS,
                source_kind=kind,
                source_rank=RANKS[kind],
                source_ref=source_ref,
                valid_from=day,
                valid_from_precision=ValidFromPrecision.EXACT
                if day
                else ValidFromPrecision.UNKNOWN,
                rule=rule,
                observed_value=observed,
            )
        )

    merged = row.get("merged_into")
    if isinstance(merged, str) and not merged.strip():
        merged = None
    if merged is not None:
        emit("merged", merged, row.get("freshness_status_updated_at"))
    if (
        table in {"decisions", "adrs"}
        and row.get("status") == "superseded"
        and row.get("superseded_by") is not None
        and row.get("successor_exists") is True
    ):
        emit(
            "superseded_decision" if table == "decisions" else "superseded_adr",
            row["superseded_by"],
            row.get("successor_created_at"),
        )
    if table == "adrs" and row.get("status") == "deprecated":
        emit("deprecated_adr", "deprecated", row.get("freshness_status_updated_at"))
    if row.get("freshness_status") == "archived" and merged is None:
        emit("decay_archived", "archived", row.get("freshness_status_updated_at"))
    return observations
