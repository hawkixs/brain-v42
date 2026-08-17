"""Unit tests for DecayCalculator — pure math, no DB."""

from __future__ import annotations

from datetime import UTC, datetime, timedelta

import pytest

from brain_v42.services.decay import DecayCalculator


@pytest.fixture
def calculator() -> DecayCalculator:
    return DecayCalculator()


class TestDecayProfile:
    def test_default_profiles_exist(self, calculator: DecayCalculator) -> None:
        """All decay-tracked entity types have profiles."""
        for t in ("decision", "learning", "snippet", "runbook", "adr", "plan"):
            assert t in calculator.profiles

    def test_plan_uses_durable_document_profile(self, calculator: DecayCalculator) -> None:
        """Plans decay with the same durable-document policy as runbooks."""
        profile = calculator.profiles["plan"]

        assert profile == calculator.profiles["runbook"]
        assert profile.age_half_life_days == 365
        assert profile.access_half_life_days == 180
        assert (profile.w_age, profile.w_access, profile.w_freq, profile.w_valid) == (
            0.2,
            0.3,
            0.3,
            0.2,
        )
        assert profile.freq_baseline == 5

    def test_profile_weights_sum_to_one(self, calculator: DecayCalculator) -> None:
        """Weights must sum to 1.0 for each profile."""
        for profile in calculator.profiles.values():
            total = profile.w_age + profile.w_access + profile.w_freq + profile.w_valid
            assert abs(total - 1.0) < 0.001


class TestComputeMultiplier:
    def test_recent_usage_can_refresh_an_old_plan(self, calculator: DecayCalculator) -> None:
        """A real recent use outweighs age for a durable plan."""
        now = datetime.now(tz=UTC)
        old = now - timedelta(days=730)

        never_used = calculator.compute_multiplier(
            entity_type="plan",
            created_at=old,
            last_accessed_at=None,
            access_count=0,
            is_validated=False,
        )
        recently_used = calculator.compute_multiplier(
            entity_type="plan",
            created_at=old,
            last_accessed_at=now,
            access_count=5,
            is_validated=False,
        )

        assert calculator.freshness_status(never_used) == "stale"
        assert calculator.freshness_status(recently_used) == "fresh"

    def test_brand_new_entity_is_fresh(self, calculator: DecayCalculator) -> None:
        """An entity created now with no accesses should have high multiplier."""
        now = datetime.now(tz=UTC)
        result = calculator.compute_multiplier(
            entity_type="decision",
            created_at=now,
            last_accessed_at=None,
            access_count=0,
            is_validated=False,
        )
        assert result > 0.5

    def test_old_never_accessed_entity_decays(self, calculator: DecayCalculator) -> None:
        """An entity created 1 year ago with no accesses should decay."""
        old = datetime.now(tz=UTC) - timedelta(days=365)
        result = calculator.compute_multiplier(
            entity_type="learning",
            created_at=old,
            last_accessed_at=None,
            access_count=0,
            is_validated=False,
        )
        assert result < 0.3

    def test_old_but_recently_accessed_stays_fresher(self, calculator: DecayCalculator) -> None:
        """An old entity accessed recently should score higher than one never accessed."""
        now = datetime.now(tz=UTC)
        old = now - timedelta(days=365)

        never_accessed = calculator.compute_multiplier(
            entity_type="learning",
            created_at=old,
            last_accessed_at=None,
            access_count=0,
            is_validated=False,
        )
        recently_accessed = calculator.compute_multiplier(
            entity_type="learning",
            created_at=old,
            last_accessed_at=now - timedelta(hours=1),
            access_count=50,
            is_validated=False,
        )
        assert recently_accessed > never_accessed

    def test_validated_entity_gets_boost(self, calculator: DecayCalculator) -> None:
        """Validated entities score higher than non-validated."""
        now = datetime.now(tz=UTC)
        old = now - timedelta(days=60)

        not_validated = calculator.compute_multiplier(
            entity_type="learning",
            created_at=old,
            last_accessed_at=None,
            access_count=0,
            is_validated=False,
        )
        validated = calculator.compute_multiplier(
            entity_type="learning",
            created_at=old,
            last_accessed_at=None,
            access_count=0,
            is_validated=True,
        )
        assert validated > not_validated

    def test_multiplier_between_zero_and_one(self, calculator: DecayCalculator) -> None:
        """Multiplier is always in [0.0, 1.0]."""
        now = datetime.now(tz=UTC)
        for entity_type in ("decision", "learning", "snippet", "runbook", "adr"):
            for days_old in (0, 30, 180, 365, 1000):
                result = calculator.compute_multiplier(
                    entity_type=entity_type,
                    created_at=now - timedelta(days=days_old),
                    last_accessed_at=None,
                    access_count=0,
                    is_validated=False,
                )
                assert 0.0 <= result <= 1.0

    def test_adr_decays_slower_than_snippet(self, calculator: DecayCalculator) -> None:
        """ADRs should decay slower than snippets (longer half-life)."""
        now = datetime.now(tz=UTC)
        old = now - timedelta(days=180)

        adr_score = calculator.compute_multiplier(
            entity_type="adr",
            created_at=old,
            last_accessed_at=None,
            access_count=0,
            is_validated=False,
        )
        snippet_score = calculator.compute_multiplier(
            entity_type="snippet",
            created_at=old,
            last_accessed_at=None,
            access_count=0,
            is_validated=False,
        )
        assert adr_score > snippet_score


class TestFreshnessStatus:
    def test_fresh_above_threshold(self, calculator: DecayCalculator) -> None:
        """Multiplier >= 0.5 → fresh."""
        assert calculator.freshness_status(0.8) == "fresh"
        assert calculator.freshness_status(0.5) == "fresh"

    def test_stale_between_thresholds(self, calculator: DecayCalculator) -> None:
        """0.2 <= multiplier < 0.5 → stale."""
        assert calculator.freshness_status(0.3) == "stale"
        assert calculator.freshness_status(0.2) == "stale"

    def test_archived_below_threshold(self, calculator: DecayCalculator) -> None:
        """Multiplier < 0.2 → archived."""
        assert calculator.freshness_status(0.1) == "archived"
        assert calculator.freshness_status(0.0) == "archived"
