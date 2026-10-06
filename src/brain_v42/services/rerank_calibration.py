"""Bind measured score thresholds to a backend rather than sharing score scales."""

from dataclasses import dataclass

import structlog

logger = structlog.get_logger(__name__)


@dataclass(frozen=True)
class RerankCalibration:
    """Keep search floors and signalling thresholds separate: neither permits a merge."""

    identity: str
    search_min_score: float
    dedup_signal: float | None
    source: str


def calibration_for_identity(identity: str) -> RerankCalibration:
    """Disable dedup signalling unless this backend's score scale is calibrated."""
    if identity == "shim":
        return RerankCalibration(
            identity=identity,
            search_min_score=0.2,
            dedup_signal=0.80,
            source="internal bench rerank-quality-2026-10-06, calibrate_min_score.py; "
            "legacy FeatureDedupJob signalling threshold",
        )
    if identity == "cohere:voyageai/rerank-3-lite":
        return RerankCalibration(
            identity=identity,
            search_min_score=0.50,
            dedup_signal=None,
            source="internal bench rerank-quality-2026-10-06, calibrate_min_score.py; "
            "no dedup calibration (ruling 9e21964f)",
        )
    logger.warning("rerank_calibration.uncalibrated", identity=identity)
    return RerankCalibration(
        identity=identity,
        search_min_score=0.2,
        dedup_signal=None,
        source="uncalibrated backend; legacy search floor fallback",
    )
