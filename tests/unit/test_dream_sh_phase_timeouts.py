"""Regression — SYNTH phase timeout headroom.

Between 2026-04-24 (commit 1411b35 exposed brain_graph_* tools to SYNTH)
and 2026-05-03, SYNTH timed out 3 nights out of 16 (04-28, 04-29, 05-02)
— all hitting the 10m ceiling exactly. Pre-04-24 SYNTH ran in 2-5 min
and never timed out. The change in tool surface plus monotonic corpus
growth pushed average duration into 6-10+ min territory, leaving zero
headroom against the original 10m budget.

This test pins the SYNTH timeout to a minimum of 15 minutes so a
careless edit cannot silently shave the headroom back below the
historical maximum observed-DONE time (8m01s on 2026-05-01) plus a
safety margin.
"""

from __future__ import annotations

import re
from pathlib import Path

REPO_ROOT = Path(__file__).resolve().parents[2]
DREAM_SH = REPO_ROOT / "scripts" / "dream.sh"


def _phase_timeout_minutes(phase: str) -> int:
    """Parse PHASES=("name:model:timeout:max_turns" ...) from dream.sh."""
    content = DREAM_SH.read_text()
    pattern = rf'"{re.escape(phase)}:\w+:(\d+):\d+"'
    match = re.search(pattern, content)
    assert match, f"phase {phase!r} not found in PHASES array of dream.sh"
    return int(match.group(1))


def test_synth_timeout_at_least_15_minutes() -> None:
    """SYNTH timed out 3× in 16 nights with the original 10m budget."""
    assert _phase_timeout_minutes("synth") >= 15, (
        "SYNTH phase timeout must be ≥15m — 10m caused 3 timeouts in 16 "
        "nights between 2026-04-24 and 2026-05-03 after brain_graph_* "
        "tools were exposed to SYNTH and corpus mega-cluster grew +27%."
    )
