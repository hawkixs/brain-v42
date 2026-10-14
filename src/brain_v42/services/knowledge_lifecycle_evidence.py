"""Serialize resumable evidence without copying entry text into run reports."""

from __future__ import annotations

import json
from collections.abc import Mapping, Sequence
from dataclasses import asdict, dataclass, field
from datetime import UTC, date, datetime, time
from enum import StrEnum
from typing import Any, Protocol, cast

from brain_v42.knowledge import extraction
from brain_v42.knowledge.checks import Sources
from brain_v42.knowledge.decide import Decision, Observation, StructuralRule
from brain_v42.knowledge.hysteresis import EvaluationRecord
from brain_v42.knowledge.ranks import Centrality, Polarity, Temporal, ValidFromPrecision


class Mode(StrEnum):
    DRY_RUN = "dry_run"
    APPLY = "apply"


class PipelineRefusal(RuntimeError):
    """A safety gate refused execution before the next side effect."""


@dataclass(frozen=True)
class BackupProof:
    run_id: str
    sealed_at: datetime


class Clock(Protocol):
    def now(self) -> datetime: ...


class Gates(Protocol):
    """Check target identity and the caller's killswitch policy on every use."""

    async def check(self, mode: str) -> str | None: ...


@dataclass(frozen=True)
class EvaluationSources:
    sources: Sources
    # Names are check kinds (e.g. code_path) or kind:ref (e.g. fact:ci.system).
    # An empty tree is a measured absence only when its source did not fail.
    failed: frozenset[str] = frozenset()


class SourcesProvider(Protocol):
    async def snapshot(self, day: date) -> EvaluationSources: ...


class ExtractionClient(Protocol):
    """Adapters wrap extraction.count/submit/collect without owning checkpoints."""

    async def count(self, manifest: extraction.Manifest) -> extraction.TokenCounts: ...

    async def submit(self, manifest: extraction.Manifest) -> str: ...

    async def collect(
        self, batch_id: str, manifest: extraction.Manifest
    ) -> extraction.CollectedResults: ...


@dataclass(frozen=True)
class RunReport:
    run_id: str
    mode: str
    status: str
    applied_by_state: dict[str, int] = field(default_factory=dict)
    applied_by_reason: dict[str, int] = field(default_factory=dict)
    compensated_by_state: dict[str, int] = field(default_factory=dict)
    compensated_by_reason: dict[str, int] = field(default_factory=dict)
    pending_by_state: dict[str, int] = field(default_factory=dict)
    pending_by_reason: dict[str, int] = field(default_factory=dict)
    deferred_by_cause: dict[str, int] = field(default_factory=dict)
    screened_entries: list[str] = field(default_factory=list)
    request_statuses: dict[str, str] = field(default_factory=dict)
    real_cost_usd: float = 0
    token_counts: dict[str, int] = field(default_factory=dict)
    worst_case_usd: float = 0
    usage: dict[str, int] = field(default_factory=dict)
    runtime_seconds: float = 0
    spot_checks: list[str] = field(default_factory=list)
    backup_proof: str | None = None
    backup_run_id: str | None = None
    backup_sealed_at: str | None = None
    failure_reason: str | None = None
    offending_ids: list[str] = field(default_factory=list)

    def to_json(self) -> str:
        return json.dumps(asdict(self), sort_keys=True)


def utc(value: datetime) -> datetime:
    if value.tzinfo is None:
        raise PipelineRefusal("clock_requires_timezone")
    return value.astimezone(UTC)


def chunk_window_open(value: datetime) -> bool:
    return not time(0, 45) <= utc(value).time().replace(tzinfo=None) < time(4)


def entry_text(row: Mapping[str, Any]) -> extraction.EntryText:
    """Use the same canonical serialization at extraction, evaluation and apply."""
    return extraction.EntryText(
        row["entity_type"],
        str(row["id"]),
        json.dumps(row["text_fields"], sort_keys=True, separators=(",", ":"), ensure_ascii=False),
        utc(row["created_at"]).date(),
    )


def pack_observation(observation: Observation) -> dict[str, Any]:
    """The JSON value retains decision metadata absent from 066's scalar columns."""
    return {
        "assertion_key": observation.assertion_key,
        "polarity": observation.polarity.value,
        "source_kind": observation.source_kind,
        "source_rank": observation.source_rank,
        "source_ref": observation.source_ref,
        "observed_value": {
            "value": observation.observed_value,
            "observation_id": observation.id,
            "centrality": observation.centrality,
            "temporal": observation.temporal,
            "rule": observation.rule,
            "propagated_from": observation.propagated_from,
        },
        "valid_from": observation.valid_from,
        "valid_from_precision": observation.valid_from_precision.value,
    }


def unpack_observation(row: Mapping[str, Any]) -> Observation:
    value = row["observed_value"]
    metadata = value if isinstance(value, dict) else {}
    return Observation(
        str(metadata.get("observation_id", row.get("id", row["source_ref"]))),
        row["assertion_key"],
        Polarity(row["polarity"]),
        row["source_kind"],
        row["source_rank"],
        row["source_ref"],
        Centrality(metadata["centrality"]) if metadata.get("centrality") else None,
        Temporal(metadata["temporal"]) if metadata.get("temporal") else None,
        row["valid_from"],
        ValidFromPrecision(row["valid_from_precision"]),
        cast(StructuralRule | None, metadata.get("rule")),
        metadata.get("propagated_from"),
        metadata.get("value", value),
    )


def record(row: Mapping[str, Any], decision: Decision) -> EvaluationRecord:
    return EvaluationRecord(
        row["observed_day"],
        row["entry_digest"],
        row["fingerprint"],
        row["complete"],
        row.get("invalidated", False),
        decision.layer,
    )


def pack_manifest(manifest: extraction.Manifest) -> dict[str, Any]:
    return {
        "requests": [
            {
                "custom_id": r.custom_id,
                "entries": [asdict(e) for e in r.entries],
                "params": r.params,
                "fact_domains": {
                    k: sorted(v) if v is not None else None for k, v in r.fact_domains.items()
                },
            }
            for r in manifest.requests
        ],
        "screened": [asdict(e) for e in manifest.screened],
    }


def unpack_manifest(run_id: str, payload: Mapping[str, Any]) -> extraction.Manifest:
    return extraction.Manifest(
        run_id,
        tuple(
            extraction.BatchRequest(
                r["custom_id"],
                tuple(extraction.EntryRevision(**e) for e in r["entries"]),
                r["params"],
                {k: frozenset(v) if v is not None else None for k, v in r["fact_domains"].items()},
            )
            for r in payload["requests"]
        ),
        tuple(extraction.EntryRevision(**e) for e in payload["screened"]),
    )


def structural_signature(observations: Sequence[Observation]) -> list[str]:
    return sorted(o.id for o in observations)


def structural_inputs(row: Mapping[str, Any]) -> dict[str, Any]:
    """Unemitted structural inputs can change during apply as well as winning rules."""
    fields = (
        "project_key",
        "status",
        "merged_into",
        "superseded_by",
        "freshness_status",
        "freshness_status_updated_at",
        "successor_exists",
        "successor_created_at",
    )
    return {key: None if row.get(key) is None else str(row[key]) for key in fields}
