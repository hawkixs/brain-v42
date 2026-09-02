"""`is_human_actor` promised fail-closed and delivered a BLACKLIST.

THE CONTRACT WAS ALREADY WRITTEN, it was not held. The function's docstring says
"Fail-closed: an unknown or unexpanded actor is NOT human"; the comment two lines
above admits the opposite — "an actor absent from this list and not a sentinel is
treated as human". This batch ALIGNS the code with its own promise; it is not a
change of intent.

WHAT SLIPPED THROUGH. `_SYSTEM_ACTOR_PREFIXES` enumerated `dream-codex-` and it
alone, while **three** dream rails are wired in `dream.sh` and each emits its own
`X-Brain-Agent`:

    codex_runner.py:125    dream-codex-{phase}     recognised
    claude_runner.py:105   dream-claude-{phase}    classified HUMAN
    agy_runner.py:116      dream-agy-{phase}       classified HUMAN

`agy` is the documented FALLBACK rail (`reorg_events.py`: "agy is the fallback").
Two rails out of three therefore counted their nightly re-reads as human reads.

MEASURED on 2026-08-22, and that is what surfaced the defect: out of 44
unarchivals in 18 days, 10 carried a "human read" timestamped between **04:03 and
05:21 UTC**, each one to two minutes before the unarchival it caused. A human does
not read the corpus at 4 a.m., six nights in a row, 90 seconds before each flusher
cycle.

THE SHAPE OF THE FIX, AND WHY IT IS NOT A HUMAN ALLOWLIST. Humans cannot be
enumerated: their actor is the project basename (`red-lab`, `brain_v42`, …),
arbitrary by construction. Requiring a human to declare themselves would break the
legitimate case — the classic trap of "closing the hole by breaking the case you
are protecting". What IS enumerable is the system FAMILY: the three rails share
the `dream-` prefix, and any future rail will share it too since that is the
runner's template. The guard therefore moves from enumerating ONE rail to
recognising the FAMILY.

AND IT IS THE STRUCTURAL TEST THAT CARRIES THE GUARANTEE, not the prefix. A fourth
`dream-<name>-{phase}` rail will be classified machine by the prefix, and
`test_every_dream_rail_header_is_machine` VERIFIES it by re-reading the runners:
if someone adds a runner emitting something else, the test reddens. That is the
witness that would have caught the original defect.

WHAT THIS BATCH DOES NOT DO. It has **no retroactive effect**: `access_log` is
drained at every flush (measured at 0 rows on 2026-08-22), so the actor history no
longer exists and `access_count_human` keeps its contamination. The fix stops the
bleeding, it does not repair the past.
"""

from __future__ import annotations

import re
from pathlib import Path

from brain_v42.provenance import UNEXPANDED_ACTOR, UNKNOWN_ACTOR, is_human_actor

_DREAM_DIR = Path(__file__).resolve().parents[2] / "scripts" / "dream"
_HEADER_LITERAL = re.compile(r'f"(dream-[a-z0-9]+-)\{phase\}"')


def _emitted_prefixes() -> set[str]:
    """The actor prefixes the dream runners REALLY emit."""
    found: set[str] = set()
    for path in sorted(_DREAM_DIR.glob("*.py")):
        found.update(_HEADER_LITERAL.findall(path.read_text(encoding="utf-8")))
    return found


class TestEveryDreamRailIsMachine:
    def test_the_three_wired_rails_are_machine(self) -> None:
        """By name, so that a regression on any one of them can be read."""
        assert is_human_actor("dream-codex-synth") is False
        assert is_human_actor("dream-claude-promote") is False
        assert is_human_actor("dream-agy-reorg") is False

    def test_every_dream_rail_header_is_machine(self) -> None:
        """THE guard: re-reads the runners and requires EVERY emitted header to be machine.

        It is this test — not the prefix — that stops a fourth rail from slipping
        through again. It would have reddened the day `claude_runner` was written.
        """
        prefixes = _emitted_prefixes()
        assert len(prefixes) >= 3, f"motif cassé, rails trouvés : {prefixes}"
        for prefix in sorted(prefixes):
            actor = f"{prefix}somephase"
            assert is_human_actor(actor) is False, (
                f"le rail {prefix!r} est émis par un runner dream mais compté HUMAIN"
            )

    def test_an_unknown_future_rail_is_machine(self) -> None:
        """Fail-closed on the family: a rail that does not yet exist."""
        assert is_human_actor("dream-mistral-extract") is False
        assert is_human_actor("dream-whatever-42") is False


class TestTheLegitimateCaseSurvives:
    """NEGATIVE WITNESS. Without it, classifying everything machine would make the class above green."""

    def test_a_real_human_session_stays_human(self) -> None:
        assert is_human_actor("red-lab") is True
        assert is_human_actor("brain_v42") is True
        assert is_human_actor("brain-v42") is True

    def test_a_project_name_that_merely_mentions_dream_stays_human(self) -> None:
        """The guard bears on the PREFIX, not on the presence of the word.

        A project named `daydream` or `dreamhouse` is a human. A substring guard
        would lose it, and that would be the same mistake in the other direction.
        """
        assert is_human_actor("daydream") is True
        assert is_human_actor("dreamhouse") is True
        assert is_human_actor("my-dream-project") is True

    def test_the_sentinels_stay_non_human(self) -> None:
        assert is_human_actor(UNKNOWN_ACTOR) is False
        assert is_human_actor(UNEXPANDED_ACTOR) is False
        assert is_human_actor("") is False
        assert is_human_actor(None) is False


class TestThisAloneClosesTheQ1Guard:
    """Is fixing `is_human_actor` ENOUGH to close the three rails? Proved, not asserted.

    The Q1 guard (`b96acad`) reads `stats["count_human"]`, which
    `PgAccessLogRepo.aggregate_in_session` builds by calling `is_human_actor` on
    `access_log.actor`. The chain is therefore:

        actor -> is_human_actor -> count_human -> unarchive_is_robot_only

    This test replays that exact composition for the three rails. If it holds, no
    additional guard is needed — and that is the question the mandate explicitly
    asks.
    """

    @staticmethod
    def _count_human(actors: list[str]) -> int:
        """Reproduces `aggregate_in_session`'s per-actor folding (one read each)."""
        return sum(1 for a in actors if is_human_actor(a))

    def test_a_night_from_any_rail_leaves_the_guard_shut(self) -> None:
        from brain_v42.services.decay_flusher import unarchive_is_robot_only

        for rail in sorted(_emitted_prefixes()):
            actors = [f"{rail}reorg", f"{rail}synth", f"{rail}promote"]
            human = self._count_human(actors)
            assert human == 0, f"{rail!r} alimente encore count_human ({human})"
            assert (
                unarchive_is_robot_only(
                    old_status="archived", new_status="fresh", human_reads=human
                )
                is True
            ), f"la garde Q1 laisserait {rail!r} désarchiver"

    def test_a_real_human_in_the_same_batch_still_opens_it(self) -> None:
        """NEGATIVE WITNESS of the composition.

        Without it, an `is_human_actor` returning False for EVERYTHING would leave
        the previous test green while removing the human right to unarchive.
        """
        from brain_v42.services.decay_flusher import unarchive_is_robot_only

        actors = ["dream-agy-reorg", "dream-codex-synth", "red-lab"]
        human = self._count_human(actors)
        assert human == 1
        assert (
            unarchive_is_robot_only(old_status="archived", new_status="fresh", human_reads=human)
            is False
        )
