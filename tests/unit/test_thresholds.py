"""Tests for the brain-v42 threshold registry (insight 87ab5d3b).

The registry is documentation-as-code: every cosine / similarity / score
threshold the corpus implicitly depends on lives in one place, with
calibration provenance attached. Sites still hold their own constants;
this module is the central inventory used to drive future calibration
sweeps and the `brain_threshold_distance_to_p95` observability gauge.
"""

from __future__ import annotations

import dataclasses

import pytest

from brain_v42 import thresholds


class TestRegistry:
    def test_registry_not_empty(self) -> None:
        assert len(thresholds.REGISTRY) > 0

    def test_each_entry_is_threshold_spec(self) -> None:
        for spec in thresholds.REGISTRY:
            assert isinstance(spec, thresholds.ThresholdSpec)

    def test_each_value_in_unit_interval(self) -> None:
        """All registered thresholds operate on a 0-1 scale."""
        for spec in thresholds.REGISTRY:
            assert 0.0 <= spec.value <= 1.0, f"{spec.name}={spec.value} out of [0,1]"

    def test_each_scale_is_known(self) -> None:
        valid = {"cosine", "reranker_score", "decay_multiplier"}
        for spec in thresholds.REGISTRY:
            assert spec.scale in valid, f"{spec.name}: unknown scale {spec.scale!r}"

    def test_names_are_unique(self) -> None:
        names = [s.name for s in thresholds.REGISTRY]
        assert len(names) == len(set(names))

    def test_known_thresholds_present(self) -> None:
        names = {s.name for s in thresholds.REGISTRY}
        # The 5 named in insight 87ab5d3b plus the 3 discovered during cataloguing
        for required in (
            "promote_dedup_cosine",
            "clean_merge_similarity",
            "auto_linker_default",
            "search_min_score",
            "promote_dedup_search_min_score",
        ):
            assert required in names, f"missing required threshold {required!r}"

    def test_calibrated_entries_have_provenance(self) -> None:
        """If calibrated=True, both last_calibrated and calibration_script must be set."""
        for spec in thresholds.REGISTRY:
            if spec.calibrated:
                assert spec.last_calibrated is not None, (
                    f"{spec.name}: calibrated but no last_calibrated date"
                )
                assert spec.calibration_script is not None, (
                    f"{spec.name}: calibrated but no calibration_script path"
                )

    def test_promote_dedup_is_only_calibrated_today(self) -> None:
        """As of 2026-04-28, only the PROMOTE dedup has run-discipline calibration.

        This test is intentionally fragile — when another threshold gets a
        calibration script + script run, update the assertion. The drift is
        the signal.
        """
        calibrated = thresholds.list_calibrated()
        assert len(calibrated) == 1
        assert calibrated[0].name == "promote_dedup_cosine"


class TestListUncalibrated:
    def test_returns_only_uncalibrated(self) -> None:
        uncalibrated = thresholds.list_uncalibrated()
        for spec in uncalibrated:
            assert not spec.calibrated

    def test_returns_at_least_5(self) -> None:
        """Insight 87ab5d3b: ≥4 uncalibrated thresholds (auto_linker, dataset
        dedup, search_min_score, plus clean variants). Cataloguing confirmed
        this floor — never let it shrink without running calibration."""
        uncalibrated = thresholds.list_uncalibrated()
        assert len(uncalibrated) >= 5


class TestThresholdSpec:
    def test_is_frozen(self) -> None:
        """ThresholdSpec must be immutable — registry is config not state."""
        spec = thresholds.REGISTRY[0]
        with pytest.raises(dataclasses.FrozenInstanceError):
            spec.value = 0.99  # type: ignore[misc]


@pytest.mark.parametrize("value", [-0.01, 1.01])
def test_threshold_spec_rejects_value_outside_unit_interval(value: float) -> None:
    with pytest.raises(ValueError, match="value must be between 0 and 1"):
        dataclasses.replace(thresholds.REGISTRY[0], value=value)


def test_threshold_spec_rejects_unknown_scale() -> None:
    with pytest.raises(ValueError, match="unknown threshold scale: distance"):
        dataclasses.replace(thresholds.REGISTRY[0], scale="distance")


@pytest.mark.parametrize("missing_field", ["last_calibrated", "calibration_script"])
def test_calibrated_threshold_requires_complete_provenance(missing_field: str) -> None:
    with pytest.raises(
        ValueError,
        match="calibrated threshold requires last_calibrated and calibration_script",
    ):
        dataclasses.replace(thresholds.REGISTRY[0], **{missing_field: None})


def test_by_name_returns_none_for_unknown_threshold() -> None:
    assert thresholds.by_name("missing-threshold") is None


def test_print_table_renders_every_registry_entry(
    capsys: pytest.CaptureFixture[str],
) -> None:
    thresholds._print_table()

    lines = capsys.readouterr().out.splitlines()
    assert lines[0].split() == ["name", "value", "scale", "calibrated", "location"]
    assert len(lines) == len(thresholds.REGISTRY) + 2
    assert any(
        line.startswith("promote_dedup_cosine") and "0.850" in line and "yes" in line
        for line in lines
    )


def test_cross_project_resonance_threshold_present():
    spec = thresholds.by_name("cross_project_resonance_min")
    assert spec is not None
    assert spec.value == 0.80
    assert spec.scale == "cosine"
    assert spec.calibrated is False


def test_ticket_extract_corpus_dedup_threshold_is_governed():
    spec = thresholds.by_name("ticket_extract_corpus_dedup_cosine")
    assert spec is not None
    assert spec.value == 0.85
    assert spec.scale == "cosine"
    assert spec.calibrated is False
    assert spec.last_calibrated is None
