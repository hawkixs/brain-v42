"""Pin measured thresholds so changing a backend cannot reuse another scale."""

from dataclasses import FrozenInstanceError

import pytest
from structlog.testing import capture_logs

from brain_v42.services.rerank_wire import CohereRerankWire, ShimRerankWire


def test_shim_calibration_preserves_existing_thresholds() -> None:
    from brain_v42.services.rerank_calibration import calibration_for_identity

    cal = calibration_for_identity("shim")
    assert cal.identity == "shim"
    assert cal.search_min_score == 0.2
    assert cal.dedup_signal == 0.80
    assert cal.source


def test_voyage_calibration_has_no_dedup_signal() -> None:
    from brain_v42.services.rerank_calibration import calibration_for_identity

    cal = calibration_for_identity("cohere:voyageai/rerank-3-lite")
    assert cal.identity == "cohere:voyageai/rerank-3-lite"
    assert cal.search_min_score == 0.50
    assert cal.dedup_signal is None
    assert "rerank-quality-2026-10-06" in cal.source


def test_unknown_identity_fails_safe_and_warns_once() -> None:
    from brain_v42.services.rerank_calibration import calibration_for_identity

    with capture_logs() as records:
        cal = calibration_for_identity("cohere:unknown-model")
    assert cal.identity == "cohere:unknown-model"
    assert cal.search_min_score == 0.2
    assert cal.dedup_signal is None
    assert records == [
        {
            "event": "rerank_calibration.uncalibrated",
            "identity": "cohere:unknown-model",
            "log_level": "warning",
        }
    ]


def test_wire_identities_match_calibration_keys() -> None:
    assert ShimRerankWire().identity == "shim"
    assert CohereRerankWire("voyageai/rerank-3-lite").identity == "cohere:voyageai/rerank-3-lite"


def test_calibration_is_frozen() -> None:
    from brain_v42.services.rerank_calibration import calibration_for_identity

    cal = calibration_for_identity("shim")
    with pytest.raises(FrozenInstanceError):
        cal.dedup_signal = None
