"""Decay score calculator — pure math, no I/O."""

from __future__ import annotations

import math
from dataclasses import dataclass, field
from datetime import UTC, datetime


@dataclass(frozen=True)
class DecayProfile:
    """Decay parameters for one entity type."""

    age_half_life_days: float
    access_half_life_days: float
    w_age: float
    w_access: float
    w_freq: float
    w_valid: float
    freq_baseline: int


_DEFAULT_PROFILES: dict[str, DecayProfile] = {
    "decision": DecayProfile(180, 90, 0.3, 0.3, 0.2, 0.2, 10),
    "learning": DecayProfile(90, 60, 0.3, 0.3, 0.2, 0.2, 10),
    "snippet": DecayProfile(60, 30, 0.2, 0.3, 0.3, 0.2, 20),
    "runbook": DecayProfile(365, 180, 0.2, 0.3, 0.3, 0.2, 5),
    "plan": DecayProfile(365, 180, 0.2, 0.3, 0.3, 0.2, 5),
    "adr": DecayProfile(730, 365, 0.1, 0.2, 0.2, 0.5, 3),
}


def _exp_decay(half_life_days: float, days_elapsed: float) -> float:
    """Exponential decay: returns value in [0.0, 1.0]."""
    if half_life_days <= 0:
        return 0.0
    lam = math.log(2) / half_life_days
    return math.exp(-lam * max(days_elapsed, 0.0))


@dataclass
class DecayCalculator:
    """Compute composite decay multiplier for entities."""

    profiles: dict[str, DecayProfile] = field(default_factory=lambda: dict(_DEFAULT_PROFILES))
    stale_threshold: float = 0.5
    archive_threshold: float = 0.2

    def compute_multiplier(
        self,
        entity_type: str,
        created_at: datetime,
        last_accessed_at: datetime | None,
        access_count: int,
        is_validated: bool,
    ) -> float:
        """Compute decay multiplier in [0.0, 1.0]."""
        profile = self.profiles.get(entity_type)
        if profile is None:
            return 1.0  # unknown type → no decay

        now = datetime.now(tz=UTC)
        days_since_created = (now - created_at).total_seconds() / 86400

        # Fallback: last_accessed_at = created_at when NULL
        effective_access = last_accessed_at if last_accessed_at is not None else created_at
        days_since_access = (now - effective_access).total_seconds() / 86400

        age_factor = _exp_decay(profile.age_half_life_days, days_since_created)
        access_factor = _exp_decay(profile.access_half_life_days, days_since_access)
        frequency_factor = (
            min(access_count / profile.freq_baseline, 1.0) if profile.freq_baseline > 0 else 0.0
        )
        validation_factor = 1.0 if is_validated else 0.7

        multiplier = (
            profile.w_age * age_factor
            + profile.w_access * access_factor
            + profile.w_freq * frequency_factor
            + profile.w_valid * validation_factor
        )
        return max(0.0, min(1.0, multiplier))

    def freshness_status(self, multiplier: float) -> str:
        """Map decay multiplier to freshness status."""
        if multiplier >= self.stale_threshold:
            return "fresh"
        if multiplier >= self.archive_threshold:
            return "stale"
        return "archived"
