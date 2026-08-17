"""Cosine and similarity threshold registry — see learning 87ab5d3b.

Inventory of every numeric threshold in brain-v42 whose value implicitly
depends on the corpus distribution (cosine pair distributions, reranker
output distribution, decay multipliers). Each entry carries calibration
provenance so corpus-shift events (e.g. test-pollution cleanup, Dream
PROMOTE materialisations, Spec C cross-domain bridges) can be matched
against the thresholds that need re-validation.

This module is intentionally documentation-as-code: it does NOT replace
the hard-coded values at their sites today. v2 will lift each site to
``import threshold_value(name)``; v1 captures the inventory so the
calibration sweep can be wired without first refactoring 8 call-sites.

Run ``python -m brain_v42.thresholds`` to print the registry as a table.
"""

from __future__ import annotations

from dataclasses import dataclass


@dataclass(frozen=True)
class ThresholdSpec:
    """One numeric threshold the corpus distribution implicitly drives."""

    name: str
    value: float
    location: str
    scale: str  # "cosine" | "reranker_score" | "decay_multiplier"
    calibrated: bool
    last_calibrated: str | None
    calibration_script: str | None
    corpus_dependency: str
    notes: str = ""

    def __post_init__(self) -> None:
        if not 0.0 <= self.value <= 1.0:
            raise ValueError(f"{self.name}: value must be between 0 and 1")
        if self.scale not in {"cosine", "reranker_score", "decay_multiplier"}:
            raise ValueError(f"unknown threshold scale: {self.scale}")
        if self.calibrated and (self.last_calibrated is None or self.calibration_script is None):
            raise ValueError("calibrated threshold requires last_calibrated and calibration_script")


REGISTRY: tuple[ThresholdSpec, ...] = (
    ThresholdSpec(
        name="promote_dedup_cosine",
        value=0.85,
        location="scripts/dream/phase_promote.md:40",
        scale="cosine",
        calibrated=True,
        last_calibrated="2026-04-21",
        calibration_script="scripts/dream/calibrate_dedup_threshold.py",
        corpus_dependency="cross-project ADR/runbook NULL distribution",
        notes="Re-run after corpus-shift events (PROMOTE materialisations, "
        "test cleanup). Two calibration runs (2026-04-19, 2026-04-21).",
    ),
    ThresholdSpec(
        name="promote_dedup_search_min_score",
        value=0.80,
        location="scripts/dream/phase_promote.md:37",
        scale="reranker_score",
        calibrated=False,
        last_calibrated=None,
        calibration_script=None,
        corpus_dependency="reranker output distribution for ADR/runbook queries",
        notes="Different scale from 0.85 dedup — this is brain_search min_score, "
        "agent-driven floor for the dedup query.",
    ),
    ThresholdSpec(
        name="ticket_extract_corpus_dedup_cosine",
        value=0.85,
        location="scripts/ticket_extract.py:_CORPUS_DEDUP_THRESHOLD",
        scale="cosine",
        calibrated=False,
        last_calibrated=None,
        calibration_script=None,
        corpus_dependency="same-project active learning/decision cosine distribution",
        notes="Initial floor borrowed from PROMOTE. Calibrate during the EXTRACT "
        "DRY soak before any WET flip.",
    ),
    ThresholdSpec(
        name="clean_merge_similarity",
        value=0.95,
        location="scripts/dream/phase_clean.md:15",
        scale="cosine",
        calibrated=False,
        last_calibrated=None,
        calibration_script=None,
        corpus_dependency="cluster-internal cosines",
        notes="Auto-merge floor; hand-chosen. Below this the Dream agent only "
        "flags for human review.",
    ),
    ThresholdSpec(
        name="clean_review_similarity_min",
        value=0.92,
        location="scripts/dream/phase_clean.md:22",
        scale="cosine",
        calibrated=False,
        last_calibrated=None,
        calibration_script=None,
        corpus_dependency="cluster-internal cosines",
        notes="Lower bound of the human-review band [0.92, 0.95).",
    ),
    ThresholdSpec(
        name="consolidation_similarity",
        value=0.92,
        location="src/brain_v42/services/consolidation.py:37",
        scale="cosine",
        calibrated=False,
        last_calibrated=None,
        calibration_script=None,
        corpus_dependency="cluster-internal cosines",
        notes="Default for ConsolidationService.find_candidates; coupled with "
        "clean_review_similarity_min.",
    ),
    ThresholdSpec(
        name="auto_linker_default",
        value=0.6,
        location="src/brain_v42/services/auto_linker.py:55",
        scale="cosine",
        calibrated=False,
        last_calibrated=None,
        calibration_script=None,
        corpus_dependency="full-corpus pairwise cosines",
        notes="Learning 2ddc02a3 flags this as a structural ceiling; learning "
        "87ab5d3b flags it as never re-validated against current p99 (~0.793).",
    ),
    ThresholdSpec(
        name="feature_dedup_min_similarity",
        value=0.50,
        location="src/brain_v42/services/feature_dedup_job.py:45",
        scale="cosine",
        calibrated=False,
        last_calibrated=None,
        calibration_script=None,
        corpus_dependency="feature-domain cosines",
        notes="Top-3 neighbour cutoff for feature dedup.",
    ),
    ThresholdSpec(
        name="search_min_score",
        value=0.2,
        location="src/brain_v42/services/brain_service.py:78",
        scale="reranker_score",
        calibrated=False,
        last_calibrated=None,
        calibration_script=None,
        corpus_dependency="reranker output distribution",
        notes="Default brain_search floor. Decision d3cf29e9 (2026-03-30) shows "
        "this floor caused zero-result blackouts during reranker fallback.",
    ),
    ThresholdSpec(
        name="cross_project_resonance_min",
        value=0.80,
        location="scripts/dream/cross_project_resonance.py",
        scale="cosine",
        calibrated=False,
        last_calibrated=None,
        calibration_script=None,
        corpus_dependency="cross-project decision-pair cosine distribution per domain",
        notes="Min cosine to surface a decision pair as cross-project resonance "
        "candidate. Deliberately below promote_dedup_cosine (0.85). "
        "Recalibrate after 5+ DRY_RUN nights (Spec C MVP β rollout).",
    ),
)


def list_calibrated() -> list[ThresholdSpec]:
    """Thresholds with a calibration script and at least one recorded run."""
    return [s for s in REGISTRY if s.calibrated]


def list_uncalibrated() -> list[ThresholdSpec]:
    """Thresholds whose value still rests on a hand-pick or debugging session."""
    return [s for s in REGISTRY if not s.calibrated]


def by_name(name: str) -> ThresholdSpec | None:
    """Lookup a single spec by its registry name; None if absent."""
    for s in REGISTRY:
        if s.name == name:
            return s
    return None


def _print_table() -> None:
    """Render the registry as a fixed-width table on stdout."""
    rows = [
        ("name", "value", "scale", "calibrated", "location"),
    ]
    for s in REGISTRY:
        rows.append(
            (
                s.name,
                f"{s.value:.3f}",
                s.scale,
                "yes" if s.calibrated else "no",
                s.location,
            )
        )
    widths = [max(len(r[i]) for r in rows) for i in range(len(rows[0]))]
    for i, row in enumerate(rows):
        print("  ".join(cell.ljust(widths[j]) for j, cell in enumerate(row)))
        if i == 0:
            print("  ".join("-" * w for w in widths))


if __name__ == "__main__":
    _print_table()
