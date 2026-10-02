"""Closed-vocabulary accounting for automatic claim extraction skips and failures.

Automatic extraction is best-effort: it never fails a knowledge write, so a
silent skip or a swallowed error is the only way it can go wrong unseen. Every
skip and failure is therefore counted under a FIXED label pair
(entity type, reason) and mirrored to a structured log line carrying the same
codes, because process counters reset at restart while the seven-day rollout
report needs history.

The vocabulary is deliberately small and closed. No fact name, statement or
exception message may become a label: a free-form label would make cardinality
a function of user prose. The snapshot enumerates the whole key space (zeros
included) so a reader sees the label set is bounded by construction.
"""

from __future__ import annotations

from collections import Counter
from collections.abc import Mapping
from dataclasses import dataclass
from itertools import product
from typing import Literal, get_args

import structlog

logger = structlog.get_logger(__name__)

EntityType = Literal["learning", "decision", "adr", "snippet", "runbook"]

#: Why a candidate (or a whole write) produced no automatic claim, without error.
SkipReason = Literal[
    # Reported by the pure extractor.
    "no_project_key",
    "prose_overflow",
    "sentence_overflow",
    "ambiguous",
    # Decided while persisting.
    "unregistered_fact",
    "disabled_fact",
    "quota_full",
]

#: Where an automatic failure was caught; the entry write itself still committed.
FailureReason = Literal["parser_error", "resolver_error", "persistence_error"]

_ENTITY_TYPES: tuple[EntityType, ...] = get_args(EntityType)
_SKIP_REASONS: tuple[SkipReason, ...] = get_args(SkipReason)
_FAILURE_REASONS: tuple[FailureReason, ...] = get_args(FailureReason)

_skipped: Counter[tuple[EntityType, SkipReason]] = Counter()
_failed: Counter[tuple[EntityType, FailureReason]] = Counter()


@dataclass(frozen=True, slots=True)
class ClaimExtractionSnapshot:
    """A point-in-time copy of every counter, keyed by (entity type, reason)."""

    skipped: Mapping[tuple[EntityType, SkipReason], int]
    failed: Mapping[tuple[EntityType, FailureReason], int]


def record_skip(
    entity_type: EntityType, reason: SkipReason, *, entity_id: str | None = None
) -> None:
    """Count one skip and emit the same fixed code to the retained log."""
    _skipped[(entity_type, reason)] += 1
    logger.info(
        "claim_extraction_skip", entity_type=entity_type, reason=reason, entity_id=entity_id
    )


def record_failure(
    entity_type: EntityType, reason: FailureReason, *, entity_id: str | None = None
) -> None:
    """Count one swallowed automatic failure and emit the same fixed code to the log."""
    _failed[(entity_type, reason)] += 1
    logger.warning(
        "claim_extraction_failed", entity_type=entity_type, reason=reason, entity_id=entity_id
    )


def snapshot() -> ClaimExtractionSnapshot:
    """Copy the counters over the full closed key space, zeros included."""
    return ClaimExtractionSnapshot(
        skipped={key: _skipped[key] for key in product(_ENTITY_TYPES, _SKIP_REASONS)},
        failed={key: _failed[key] for key in product(_ENTITY_TYPES, _FAILURE_REASONS)},
    )
