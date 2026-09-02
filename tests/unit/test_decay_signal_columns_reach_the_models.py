"""Schema ↔ model contract: what the decay reads from the database must reach the model.

This test exists because of a defect measured on 2026-08-22, and it is written to
make its CLASS impossible, not to pin its instance.

The defect: `decay_human_signal_enabled` substituted
`getattr(entity, "access_count_human", 0)` for the machine counters, on Pydantic
models that did not declare that field. The `getattr` therefore always fell back
on its default. The flag was not a switch between machine signal and human signal:
it was a switch between machine signal and **NOTHING** — while `DecayFlusher`, for
its part, read the real columns in SQLAlchemy Core. Arming it would have made the
two paths diverge on the same row.

**Why this test and not a behaviour test.** The suite already carried
`test_decay_human_signal.py`, which builds a `SimpleNamespace` equipped with both
attributes then copies the production logic into the test body. It proves the
code's SHAPE and cannot see the data's ARRIVAL: it is the one that let this defect
through for the whole workstream. The end-to-end proof now lives in integration
(`tests/integration/db/test_decay_human_signal_hydration.py`, against real rows).
This test is the net that runs WITHOUT a database, in CI: it derives the column
names from the real `Table` objects and the flusher's real lists — so it cannot be
wrong at the same time as them, which was exactly the failure mode.
"""

from __future__ import annotations

import pytest

from brain_v42.config import Settings
from brain_v42.db.tables import (
    adrs,
    decisions,
    indexed_plans,
    learnings,
    runbooks,
    snippets,
)
from brain_v42.models.adr import ADR
from brain_v42.models.decision import Decision
from brain_v42.models.indexed_plan import IndexedPlan
from brain_v42.models.indexed_plan_chunk import IndexedPlanChunk
from brain_v42.models.learning import Learning
from brain_v42.models.runbook import Runbook
from brain_v42.models.snippet import Snippet

#: The human-signal columns (migrations 041 and 044). They switch TOGETHER:
#: `access_count` weighs 0.2 in the formula, `last_accessed_at` 0.3 — carrying only
#: one repairs 0.2 of the 0.5 driven by reads and gives the illusion that the decay
#: is repaired.
_HUMAN_COLUMNS = ("access_count_human", "last_accessed_at_human")

#: The six tables tracked by the decay, each with the model the repositories
#: hydrate from it. `indexed_plans` is the asymmetric one: its chunks have no
#: `_human` columns, so a plan's human signal can only come from the parent.
_TABLE_TO_MODEL = (
    (decisions, Decision),
    (learnings, Learning),
    (snippets, Snippet),
    (runbooks, Runbook),
    (adrs, ADR),
    (indexed_plans, IndexedPlan),
)


@pytest.mark.parametrize(
    ("table", "model"),
    _TABLE_TO_MODEL,
    ids=[table.name for table, _ in _TABLE_TO_MODEL],
)
def test_every_human_column_has_a_model_field(table, model) -> None:
    """What the database carries, the model must declare — otherwise Pydantic drops it.

    The search projections ALREADY returned these columns (`_search_columns()` only
    excludes `embedding` and `search_vector`): the data arrived in the row and was
    lost crossing the model, for lack of a field.
    """
    for column in _HUMAN_COLUMNS:
        assert column in table.c, f"{table.name} devrait porter {column}"
        assert column in model.model_fields, (
            f"{model.__name__} ne déclare pas {column} : la colonne existe, "
            f"le SELECT la renvoie, et Pydantic la jette en silence"
        )


def test_the_plan_chunk_carries_the_parent_human_signal() -> None:
    """The only type where the human signal CANNOT come from the scored entity.

    `indexed_plan_chunks` has no `_human` column; the machine path already knows
    that and substitutes the parent's counters. Without a `parent_*_human` field on
    the chunk, the human branch scored every plan at 0 accesses and null recency —
    not a value divergence, a structural impossibility.
    """
    from brain_v42.db.tables import indexed_plan_chunks

    for column in _HUMAN_COLUMNS:
        assert column not in indexed_plan_chunks.c, (
            f"si {column} apparaît sur les chunks, ce test et la substitution "
            f"parent de brain_service doivent être revus ensemble"
        )
        assert f"parent_{column}" in IndexedPlanChunk.model_fields


def test_the_flusher_and_the_models_read_the_same_names() -> None:
    """Divergence is the danger, not absence.

    The flusher reads `table.c.access_count_human` in Core; the service reads
    `entity.access_count_human` in Pydantic. As long as the model did not carry the
    field, arming the flag gave TWO values for one row: a constant on one side, the
    data on the other. We therefore compare the names the flusher actually SELECTs
    with those the models declare, instead of copying a list by hand — a copied list
    would drift with the code it claims to guard.
    """
    import inspect

    from brain_v42.services.decay_flusher import DecayFlusher

    flusher_source = inspect.getsource(DecayFlusher)
    for column in _HUMAN_COLUMNS:
        assert f"table.c.{column}" in flusher_source, (
            f"le flusher ne lit plus {column} : si cette lecture disparaît, "
            f"c'est l'autre chemin qui devient la seule source"
        )
        for _table, model in _TABLE_TO_MODEL:
            assert column in model.model_fields


def test_the_setting_is_still_closed_by_default() -> None:
    """This batch repairs the hydration. It arms NOTHING.

    The expected visible effect is zero: making a future arming honest, not
    performing it. Opening this flag changes the order of search results the same
    day — that is an operator's gesture.
    """
    assert Settings.model_fields["decay_human_signal_enabled"].default is False


class TestTheModelReachesTheCalculator:
    """Second link: from the model to the multiplier, through the REAL loop.

    The two tests compose, and must be read together: the integration one proves the
    data goes from the DATABASE to the MODEL; this one proves it goes from the MODEL
    to the CALCULATION. Neither suffices alone, and it is precisely the missing half
    that had let the defect through.

    What distinguishes it from the forbidden pattern: it calls
    `BrainService._build_search_results`, the production method, on a real
    `IndexedPlanChunk` instance. It does not copy the substitution into its own body
    — mutation verified: removing the parent fallback from `brain_service` reddens
    this test, and nothing else in the suite saw it.
    """

    @staticmethod
    def _chunk(**overrides):
        import datetime as dt
        import uuid

        payload = {
            "id": uuid.uuid4(),
            "plan_id": uuid.uuid4(),
            "section_title": "s",
            "section_path": "s",
            "content": "c",
            "section_order": 1,
            "word_count": 1,
            "project_key": "integ-decay",
            "plan_type": "plan",
            "status": "active",
            "created_at": dt.datetime(2026, 1, 1, tzinfo=dt.UTC),
            "access_count": 7,
            "last_accessed_at": dt.datetime(2026, 8, 10, tzinfo=dt.UTC),
            "parent_access_count": 400,
            "parent_last_accessed_at": dt.datetime(2026, 8, 10, tzinfo=dt.UTC),
            "parent_created_at": dt.datetime(2026, 1, 1, tzinfo=dt.UTC),
            "parent_access_count_human": 3,
            "parent_last_accessed_at_human": dt.datetime(2026, 2, 1, tzinfo=dt.UTC),
        }
        payload.update(overrides)
        return IndexedPlanChunk.model_validate(payload)

    def _score(self, *, human_signal: bool):
        from brain_v42.services.brain_service import BrainService

        class _Recorder:
            def __init__(self) -> None:
                self.calls: list[dict] = []

            def compute_multiplier(self, **kwargs):
                self.calls.append(kwargs)
                return 1.0

            def freshness_status(self, multiplier: float) -> str:
                return "fresh"

        recorder = _Recorder()
        service = BrainService(
            decision_svc=None,
            learning_svc=None,
            snippet_svc=None,
            runbook_svc=None,
            adr_svc=None,
            embedding_svc=None,
            decay_calculator=recorder,
            decay_human_signal_enabled=human_signal,
        )
        service._build_search_results({"plan": [(self._chunk(), 0.9)]}, limit=10)
        return recorder.calls[-1]

    def test_armed_the_plan_is_scored_on_the_parent_human_counters(self) -> None:
        call = self._score(human_signal=True)
        assert call["access_count"] == 3
        assert call["last_accessed_at"].month == 2

    def test_closed_the_plan_is_scored_on_the_parent_machine_counters(self) -> None:
        """Negative witness: without it, a calculation frozen at 3 would pass too."""
        call = self._score(human_signal=False)
        assert call["access_count"] == 400
        assert call["last_accessed_at"].month == 8

    def test_armed_a_learning_is_scored_on_its_own_human_counters(self) -> None:
        """The NON-plan case, and it was missing.

        MEASURED: with the `plan` case alone, freezing the human branch on the
        machine counter left the suite GREEN — the parent substitution rewrites
        `signal_access_count` immediately afterwards, so it masked the line we
        believed we were testing. The five knowledge types have no parent: it is
        here, and only here, that this line is visible.
        """
        import datetime as dt
        import uuid

        learning = Learning.model_validate(
            {
                "id": uuid.uuid4(),
                "topic": "t",
                "insight": "i",
                "project_key": "integ-decay",
                "created_at": dt.datetime(2026, 1, 1, tzinfo=dt.UTC),
                "updated_at": dt.datetime(2026, 1, 1, tzinfo=dt.UTC),
                "access_count": 400,
                "last_accessed_at": dt.datetime(2026, 8, 10, tzinfo=dt.UTC),
                "access_count_human": 3,
                "last_accessed_at_human": dt.datetime(2026, 2, 1, tzinfo=dt.UTC),
            }
        )
        from brain_v42.services.brain_service import BrainService

        class _Recorder:
            def __init__(self) -> None:
                self.calls: list[dict] = []

            def compute_multiplier(self, **kwargs):
                self.calls.append(kwargs)
                return 1.0

            def freshness_status(self, multiplier: float) -> str:
                return "fresh"

        recorder = _Recorder()
        BrainService(
            decision_svc=None,
            learning_svc=None,
            snippet_svc=None,
            runbook_svc=None,
            adr_svc=None,
            embedding_svc=None,
            decay_calculator=recorder,
            decay_human_signal_enabled=True,
        )._build_search_results({"learning": [(learning, 0.9)]}, limit=10)

        call = recorder.calls[-1]
        assert call["access_count"] == 3
        assert call["last_accessed_at"].month == 2
