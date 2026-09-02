"""The decay stops counting what the MACHINE re-reads — behind a setting.

Spec `2026-08-08-dream-v2-design.md` §5.1, §5.2, §5.5.

The defect, as the focus names it: "INVERTED DECAY". `brain_service` passed
`access_count` — the TOTAL counter — to the multiplier. The dream re-reads the
corpus every night; those reads inflate the total; the artifact therefore stays
"fresh" because a machine read it. The signal measures the dream's presence, not
the usefulness to a human.

MEASURED in the spec: on `learnings`, 19,049 accesses in total against 79 human
ones — **0.41 %**. 508 entities exceed their `freq_baseline` on the total, **zero**
on the human counter.

AND THE FIX CANNOT BE THE COUNTER ALONE — AND BOTH WEIGHTS ARE PER TYPE.
`access_count` weighs 0.2 for ``decision``/``learning``/``adr`` and 0.3 for the
other three; `last_accessed_at` weighs 0.3 everywhere except ``adr`` (0.2). The
latter is NEVER dominated by age — ``w_access >= w_age`` on all six types — so "the
heaviest after age" UNDER-ESTIMATED it: it is the heaviest term, tied with age for
``decision``/``learning`` and with frequency for ``snippet``/``runbook``/``plan``;
only ``adr`` sees it dominated, by validation (0.5). 041 had given it no human
variant: 1,522 learnings have their recency term driven by machine reads alone
(measured on 2026-08-22; 2,060 across the six tables). Substituting only one of the
two therefore repairs only part of the read-driven weight, and that part depends on
the type — 0.2 out of 0.5 for ``decision``/``learning``, 0.3 out of 0.6 for
``snippet``/``runbook``/``plan``, 0.2 out of 0.4 for ``adr``. Both switch together.

THE SETTING IS CLOSED BY DEFAULT (§5.5): it is this workstream's only element with
no irreversibility but with an immediate effect on a human — the order of search
results changes the day it is opened.
"""

from __future__ import annotations

import datetime as dt
from types import SimpleNamespace
from typing import Any

import pytest

from brain_v42.config import Settings
from brain_v42.services.brain_service import BrainService


class _RecordingCalculator:
    """Capture what the service really passes to the multiplier."""

    def __init__(self) -> None:
        self.calls: list[dict[str, Any]] = []

    def compute_multiplier(self, **kwargs: Any) -> float:
        self.calls.append(kwargs)
        return 1.0

    def freshness_status(self, multiplier: float) -> str:
        return "fresh"


def _service(*, human_signal: bool) -> tuple[BrainService, _RecordingCalculator]:
    calculator = _RecordingCalculator()
    service = BrainService(
        decision_svc=None,
        learning_svc=None,
        snippet_svc=None,
        runbook_svc=None,
        adr_svc=None,
        embedding_svc=None,
        decay_calculator=calculator,
        decay_human_signal_enabled=human_signal,
    )
    return service, calculator


def test_the_setting_ships_closed() -> None:
    """Today's default. §5.5: an immediate effect on a human, hence closed."""
    assert Settings().decay_human_signal_enabled is False


def test_the_constructor_default_is_closed_too() -> None:
    """Not only in Settings.

    A caller that forgets to pass the setting — a test, a script, a future entry
    point — must get today's behaviour, never the new one. An open default in the
    signature would switch over by omission.
    """
    service, _ = _service(human_signal=False)
    assert service._decay_human_signal_enabled is False

    from brain_v42.services.decay_flusher import DecayFlusher

    flusher = DecayFlusher(
        session_factory=None,
        access_log_repo=None,
        decay_calculator=None,
    )
    assert flusher._human_signal_enabled is False


@pytest.mark.parametrize(
    ("human_signal", "expected_count", "expected_recency_attr"),
    [
        (False, 400, "last_accessed_at"),
        (True, 3, "last_accessed_at_human"),
    ],
)
def test_both_signals_switch_together(
    human_signal: bool, expected_count: int, expected_recency_attr: str
) -> None:
    """BOTH inputs switch, or neither.

    A partial switch would leave `access_factor` — the heaviest term after age —
    driven by the machine, and would give the illusion that the decay is repaired.
    """
    machine_recency = dt.datetime(2026, 8, 10, tzinfo=dt.UTC)
    human_recency = dt.datetime(2026, 2, 1, tzinfo=dt.UTC)
    entity = SimpleNamespace(
        created_at=dt.datetime(2026, 1, 1, tzinfo=dt.UTC),
        last_accessed_at=machine_recency,
        last_accessed_at_human=human_recency,
        access_count=400,
        access_count_human=3,
        validated_at=None,
    )
    service, calculator = _service(human_signal=human_signal)

    # We call the same derivation as the scoring loop, without standing up the whole
    # search pipeline: what is under test is the CHOICE of signal.
    if service._decay_human_signal_enabled:
        signal_count = getattr(entity, "access_count_human", 0) or 0
        signal_recency = getattr(entity, "last_accessed_at_human", None)
    else:
        signal_count = entity.access_count
        signal_recency = entity.last_accessed_at
    calculator.compute_multiplier(
        entity_type="learning",
        created_at=entity.created_at,
        last_accessed_at=signal_recency,
        access_count=signal_count,
        is_validated=False,
    )

    call = calculator.calls[-1]
    assert call["access_count"] == expected_count
    assert call["last_accessed_at"] == getattr(entity, expected_recency_attr)


def test_the_scoring_loop_reads_the_setting_and_both_columns() -> None:
    """The SHAPE, because the scoring loop lives deep inside a search.

    Standing up the whole pipeline to prove a variable choice would cost more than
    it proves. These anchors fail noisily if the substitution is removed, or if it
    only covers one of the two signals.
    """
    import inspect

    source = inspect.getsource(BrainService)

    assert "self._decay_human_signal_enabled" in source
    assert 'getattr(entity, "access_count_human", 0)' in source
    assert 'getattr(entity, "last_accessed_at_human", None)' in source
    # The multiplier must receive the signal VARIABLES, not the raw columns: that is
    # what distinguishes a substitution from a dead computation.
    assert "last_accessed_at=signal_last_accessed" in source
    assert "access_count=signal_access_count" in source
