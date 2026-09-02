"""The human signal must REACH the model, not merely exist in the database.

`decay_human_signal_enabled` was shipped as a switch between the machine signal
and the human signal. Measured on 2026-08-22, it was a switch between the machine
signal and **NOTHING**: `brain_service` reads
`getattr(entity, "access_count_human", 0)` on a **Pydantic** model that did not
declare the field, so the `getattr` always fell back on its default. The
DecayFlusher, for its part, reads the real columns in SQLAlchemy Core. Arming the
flag would have made **the two paths diverge**: one on a constant, the other on
the data.

**WHY THIS TEST IS AN INTEGRATION TEST, AND NOT A UNIT TEST.**
`tests/unit/test_decay_human_signal.py` builds a `SimpleNamespace` carrying both
attributes, then copies the production logic into the test body. It proves the
code's SHAPE and nothing else — it is the one that masked this defect for the whole
workstream. **A test that builds the object it verifies itself proves nothing about
the real path.** Here the object comes from a REAL row, read by the REAL
repository, hydrated by the REAL model. It is the only form that could have
detected the defect, and the only one that will keep it closed.
"""

from __future__ import annotations

import datetime as dt
import uuid

import pytest
import sqlalchemy as sa

from brain_v42.db.tables import learnings
from brain_v42.repositories.pg_learning import PgLearningRepo

pytestmark = pytest.mark.integration

_PROJECT = "integ-decay-human-signal"
#: Two values deliberately DIFFERENT from the machine counters: if the model fell
#: back on the total, or on a default, the assert would see it.
_HUMAN_COUNT = 3
_MACHINE_COUNT = 400
_HUMAN_RECENCY = dt.datetime(2026, 2, 1, tzinfo=dt.UTC)
_MACHINE_RECENCY = dt.datetime(2026, 8, 10, tzinfo=dt.UTC)


@pytest.fixture
async def seeded_learning_id(db_session) -> uuid.UUID:
    """Write a real row carrying the four counters, and return its id."""
    row_id = uuid.uuid4()
    await db_session.execute(
        sa.insert(learnings).values(
            id=row_id,
            project_key=_PROJECT,
            topic="hydratation du signal humain",
            insight="Une ligne réelle, pas un SimpleNamespace.",
            access_count=_MACHINE_COUNT,
            access_count_human=_HUMAN_COUNT,
            last_accessed_at=_MACHINE_RECENCY,
            last_accessed_at_human=_HUMAN_RECENCY,
        )
    )
    await db_session.commit()
    return row_id


class TestTheHumanSignalReachesTheModel:
    async def test_get_by_id_carries_both_human_columns(
        self, session_factory, seeded_learning_id: uuid.UUID
    ) -> None:
        """The single-read path — RETURNING */full projection."""
        learning = await PgLearningRepo(session_factory).get_by_id(seeded_learning_id)

        assert learning is not None
        assert learning.access_count_human == _HUMAN_COUNT
        assert learning.last_accessed_at_human == _HUMAN_RECENCY
        # NEGATIVE WITNESS, inside the test itself: without it, a model copying the
        # TOTAL counter into the human field would pass.
        assert learning.access_count == _MACHINE_COUNT
        assert learning.last_accessed_at == _MACHINE_RECENCY
        assert learning.access_count_human != learning.access_count
        assert learning.last_accessed_at_human != learning.last_accessed_at

    async def test_search_path_carries_both_human_columns(
        self, session_factory, seeded_learning_id: uuid.UUID
    ) -> None:
        """The path that REALLY matters: it is the one that feeds the decay.

        `_search_columns()` projects every column except `embedding` and
        `search_vector`, so the data already arrived in the ROW. It was thrown away
        by Pydantic, for lack of a declared field. This test pins the exact junction
        where it was lost.
        """
        repo = PgLearningRepo(session_factory)
        rows = await repo.list_all(project_key=_PROJECT, limit=10)

        found = [item for item in rows if item.id == seeded_learning_id]
        assert found, "la ligne semée doit être visible du chemin de liste"
        learning = found[0]
        assert learning.access_count_human == _HUMAN_COUNT
        assert learning.last_accessed_at_human == _HUMAN_RECENCY


class TestBothPathsReadTheSameValue:
    async def test_flusher_columns_and_model_fields_agree(
        self, db_session, session_factory, seeded_learning_id: uuid.UUID
    ) -> None:
        """Divergence is the real danger, not absence.

        The DecayFlusher reads `table.c.access_count_human` in Core; the service
        reads `entity.access_count_human` in Pydantic. As long as the model did not
        carry the field, arming the flag gave TWO values for one entity — a constant
        on one side, the data on the other. This test compares both readings on the
        same row, through both real paths.
        """
        core_row = (
            (
                await db_session.execute(
                    sa.select(
                        learnings.c.access_count_human,
                        learnings.c.last_accessed_at_human,
                    ).where(learnings.c.id == seeded_learning_id)
                )
            )
            .mappings()
            .one()
        )

        model = await PgLearningRepo(session_factory).get_by_id(seeded_learning_id)
        assert model is not None

        assert model.access_count_human == core_row["access_count_human"]
        assert model.last_accessed_at_human == core_row["last_accessed_at_human"]


class TestThePlanPathCarriesTheParentHumanSignal:
    """Plans are the SIXTH type tracked by the decay, and the only asymmetric one.

    Only the PARENT (`indexed_plans`) carries the human columns; the chunks do not
    (verified in `tables.py`). The machine path already knows that and substitutes
    the parent's counters (`brain_service`, the `t == "plan"` branch). The human
    branch, for its part, read the attribute on the CHUNK — not found, hence `0` and
    `None` **for every plan, always**. It was not merely a divergence from the
    flusher: it was structurally unreachable without the parent join.
    """

    @pytest.fixture
    async def seeded_plan(self, db_session) -> uuid.UUID:
        from brain_v42.db.tables import indexed_plan_chunks, indexed_plans

        plan_id = uuid.uuid4()
        await db_session.execute(
            sa.insert(indexed_plans).values(
                id=plan_id,
                file_path=f"/tmp/{plan_id}.md",
                title="plan de sonde du signal humain",
                plan_type="plan",
                project_key=_PROJECT,
                content_hash=uuid.uuid4().hex,
                access_count=_MACHINE_COUNT,
                access_count_human=_HUMAN_COUNT,
                last_accessed_at=_MACHINE_RECENCY,
                last_accessed_at_human=_HUMAN_RECENCY,
            )
        )
        await db_session.execute(
            sa.insert(indexed_plan_chunks).values(
                id=uuid.uuid4(),
                plan_id=plan_id,
                section_title="hydratation",
                section_path="hydratation",
                content="Le signal humain du plan vient du parent, jamais du chunk.",
                section_order=1,
                word_count=10,
                embedding=[0.0] * 1536,
                project_key=_PROJECT,
                plan_type="plan",
                status="active",
                # `search_vector` is NOT a generated column on the chunks: without
                # setting it, the FTS `@@` matches nothing and the test would redden
                # for a reason other than the one it targets.
                search_vector=sa.func.to_tsvector(
                    "english",
                    "Le signal humain du plan vient du parent, jamais du chunk.",
                ),
            )
        )
        await db_session.commit()
        return plan_id

    async def test_chunk_carries_the_parent_human_counters(
        self, session_factory, seeded_plan: uuid.UUID
    ) -> None:
        from brain_v42.services.indexed_plan_search_service import IndexedPlanSearchService

        results = await IndexedPlanSearchService(session_factory).search(
            query="parent",
            project_key=_PROJECT,
            limit=10,
        )

        chunks = [chunk for chunk in results if chunk.plan_id == seeded_plan]
        assert chunks, "le chunk semé doit être visible du chemin de recherche"
        chunk = chunks[0]
        assert chunk.parent_access_count_human == _HUMAN_COUNT
        assert chunk.parent_last_accessed_at_human == _HUMAN_RECENCY
        # NEGATIVE WITNESS: the machine parent stays distinct from the human parent.
        assert chunk.parent_access_count == _MACHINE_COUNT
        assert chunk.parent_access_count_human != chunk.parent_access_count
