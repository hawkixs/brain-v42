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


def is_calibrated(identity: str) -> bool:
    """A fallback needs a measured floor before its scale can be aligned."""
    return identity in {
        "shim",
        "cohere:voyageai/rerank-3-lite",
        "cohere:cohere/rerank-4-fast",
        "voyage:rerank-3-lite",
    }


def calibration_for_identity(identity: str) -> RerankCalibration:
    """Disable dedup signalling unless this backend's score scale is calibrated."""
    if identity == "voyage:rerank-3-lite":
        return RerankCalibration(
            identity=identity,
            search_min_score=0.50,
            dedup_signal=None,
            source="same model as cohere:voyageai/rerank-3-lite (internal bench, 762 queries)",
        )
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
    if identity == "cohere:cohere/rerank-4-fast":
        return RerankCalibration(
            identity=identity,
            search_min_score=0.30,
            dedup_signal=None,
            source="internal bench rerank-quality-2026-10-06 full 762 (internal #23), "
            "calibrate_min_score.py CAL_MODEL",
        )
    logger.warning("rerank_calibration.uncalibrated", identity=identity)
    return RerankCalibration(
        identity=identity,
        search_min_score=0.2,
        dedup_signal=None,
        source="uncalibrated backend; legacy search floor fallback",
    )
