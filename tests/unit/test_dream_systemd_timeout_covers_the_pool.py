"""The systemd cap covers the pool, and it is DERIVED, not retyped.

Spec `2026-08-08-dream-project-pool-design.md` §4.3 and §10.

`TimeoutStartSec` must be set on bound (b) — the CONFIGURED cap — and not on the
measured average, because systemd kills at the bound. The calculation:

    N × (sum of the phase timeouts) + retry budget + global phases

At 180 min (10800 s), the pre-pool value, **two projects were enough to
overshoot**: 2 × 53 + 43 + 35 = 227 min. The night would be killed in the middle
of the second project, and the following projects would have NO row at all in
`dream_runs` — invisible to a `DISTINCT ON (phase)` reader that would see the
phases of the projects already processed.

This test retypes no number. It reads the real timeouts from `PHASES` and from
the three `timeout Nm` of the global phases, then checks that the versioned
template covers `_MAX_POOL` projects. A phase timeout raised without raising the
cap fails here, naming what is missing.

AND IT CHECKS THE TEMPLATE, NOT THE LIVE UNIT. `deploy/systemd/install.sh`
regenerates the unit from the template, and its guardrail only warns about
`Environment=` lines added by hand — `TimeoutStartSec` is not one of them. A cap
raised by hand in ~/.config/systemd/user/ would be rewritten at the next
reinstall, without a word. This is the twin of the 2026-06-30 incident
(PROMOTE+REORG switched off for two nights by a regeneration).
"""

from __future__ import annotations

import re
from pathlib import Path

REPO_ROOT = Path(__file__).resolve().parents[2]
DREAM_SH = REPO_ROOT / "scripts" / "dream.sh"
TEMPLATE = REPO_ROOT / "deploy" / "systemd" / "brain-v42-dream.service.tmpl"

# Pool size the versioned cap must sustain. The "10 big ones" of decision
# 4158d142. Widening beyond that is a number to change HERE first — and this test
# is what forces you to notice.
_MAX_POOL = 10


def _agent_phase_minutes() -> int:
    """Sum of the six agent phases' timeouts, read from PHASES."""
    content = DREAM_SH.read_text(encoding="utf-8")
    entries = re.findall(r'"(\w+):(?:fast|deep):(\d+):\d+"', content)
    assert len(entries) == 6, f"attendu 6 phases agent dans PHASES, mesuré {len(entries)}"
    return sum(int(minutes) for _, minutes in entries)


def _global_phase_minutes() -> int:
    """Sum of the global phases' three `timeout Nm`."""
    content = DREAM_SH.read_text(encoding="utf-8")
    total = 0
    for module in ("scripts.ticket_extract", "scripts.roadmap_curate", "session_sweep"):
        match = re.search(rf"timeout (\d+)m uv run python -m [\w.]*{re.escape(module)}", content)
        assert match, f"garde-fou `timeout Nm` introuvable pour {module}"
        total += int(match.group(1))
    return total


def _retry_budget() -> int:
    content = DREAM_SH.read_text(encoding="utf-8")
    match = re.search(r'BRAIN_DREAM_RETRY_BUDGET="\$\{BRAIN_DREAM_RETRY_BUDGET:-(\d+)\}"', content)
    assert match, "BRAIN_DREAM_RETRY_BUDGET introuvable dans dream.sh"
    return int(match.group(1))


def _longest_retriable_phase_minutes() -> int:
    """The most expensive phase that can be retried.

    PROMOTE is explicitly excluded from retry, and a timeout is never retried —
    only a hard failure is.
    """
    content = DREAM_SH.read_text(encoding="utf-8")
    entries = re.findall(r'"(\w+):(?:fast|deep):(\d+):\d+"', content)
    return max(int(minutes) for name, minutes in entries if name != "promote")


def _template_timeout_seconds() -> int:
    content = TEMPLATE.read_text(encoding="utf-8")
    match = re.search(r"^TimeoutStartSec=(\d+)$", content, re.MULTILINE)
    assert match, "TimeoutStartSec introuvable dans le template versionné"
    return int(match.group(1))


def test_the_versioned_template_covers_the_configured_worst_case() -> None:
    ceiling_minutes = (
        _MAX_POOL * _agent_phase_minutes()
        + _retry_budget() * _longest_retriable_phase_minutes()
        + _global_phase_minutes()
    )

    assert _template_timeout_seconds() >= ceiling_minutes * 60, (
        f"TimeoutStartSec={_template_timeout_seconds()}s ne couvre pas le pire cas "
        f"configuré à {_MAX_POOL} projets ({ceiling_minutes} min = "
        f"{ceiling_minutes * 60}s). systemd tuerait la nuit au milieu d'un projet, "
        "et les projets suivants n'auraient aucune ligne dans dream_runs."
    )


def test_the_old_ceiling_could_not_have_served_the_intended_pool() -> None:
    """How many projects did the old cap cover? Derived, not retyped.

    §4.3 puts it at "227 min at two projects, already over". That number was right
    UNDER THE OLD retry REGIME, where each project carried its own +43 eligible
    min. The night-wide allocation shipped with the loop bought that margin back:
    at two projects we are now at 171 min, under 180.

    The old cap therefore breaks at THREE projects, not two. The spec's conclusion
    holds — it could not serve the ten — but its figure describes a script that
    has changed since. We measure.
    """
    fixed = _retry_budget() * _longest_retriable_phase_minutes() + _global_phase_minutes()
    per_project = _agent_phase_minutes()
    covered = (10800 // 60 - fixed) // per_project

    assert covered < _MAX_POOL, (
        f"l'ancien plafond de 180 min couvrirait {covered} projets, soit le pool "
        f"visé de {_MAX_POOL} : l'arithmétique de §4.3 est à remesurer"
    )
    assert covered >= 1, (
        "l'ancien plafond ne couvrirait plus même un projet — les timeouts de "
        "phase ont explosé et c'est ça qu'il faut regarder, pas le plafond"
    )


def test_the_ceiling_is_not_absurdly_oversized() -> None:
    """A cap must stay a cap, not an absence of cap.

    The timer is daily: beyond 24 h, a night could overlap the next one — and
    `Type=oneshot` then LOSES the trigger, with no queue and no error.
    """
    assert _template_timeout_seconds() < 24 * 3600
