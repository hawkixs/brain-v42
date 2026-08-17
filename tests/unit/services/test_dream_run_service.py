"""Tests for DreamRunService — killswitch_state + last_failure."""

from __future__ import annotations

from datetime import UTC, date, datetime, timedelta

import pytest
import sqlalchemy as sa
from sqlalchemy import (
    Boolean,
    Column,
    Date,
    DateTime,
    Float,
    Integer,
    MetaData,
    String,
    Table,
    Text,
)
from sqlalchemy.ext.asyncio import AsyncSession, async_sessionmaker, create_async_engine

from brain_v42.dream_degradation import DEGRADED_PREFIX
from brain_v42.services.dream_run_service import DreamRunService

# Formes RÉELLES lues dans dream_runs.error_message le 2026-08-11. Les deux
# vivent sur des runs status='done' : c'est ce qui rend le prédicat délicat.
_DEGRADED_MESSAGE = (
    f"{DEGRADED_PREFIX} : 10/10 batches servis par le modèle de SECOURS meta/llama-3.1-8b-instruct"
)
_INFORMATIVE_MESSAGE = "15 ticket(s) deferred or timed out before run deadline"

# Local SQLite-friendly mirror of brain_v42.db.tables.dream_runs.
# Mirrors prod columns minus PG-only types. Drift risk acknowledged.
_TEST_METADATA = MetaData()
_dream_runs = Table(
    "dream_runs",
    _TEST_METADATA,
    Column("id", Integer, primary_key=True, autoincrement=True),
    Column("run_date", Date, nullable=False),
    Column("phase", String(10), nullable=False),
    Column("model", String(120)),
    Column("status", String(10), nullable=False),
    Column("duration_s", Float),
    Column("input_tokens", Integer, server_default="0"),
    Column("output_tokens", Integer, server_default="0"),
    Column("cache_read_tokens", Integer, server_default="0"),
    Column("cache_creation_tokens", Integer, server_default="0"),
    Column("cost_usd", Float, server_default="0"),
    Column("api_calls", Integer, server_default="0"),
    Column("tool_calls", Integer, server_default="0"),
    Column("error_message", Text, nullable=True),
    Column("phase_dry_run", Boolean, nullable=False, server_default=sa.text("0")),
    Column("created_at", DateTime, server_default=sa.func.now()),
)


@pytest.fixture
async def session_factory():
    engine = create_async_engine("sqlite+aiosqlite:///:memory:", future=True)
    async with engine.begin() as conn:
        await conn.run_sync(_TEST_METADATA.create_all)
    factory = async_sessionmaker(engine, class_=AsyncSession, expire_on_commit=False)
    yield factory
    await engine.dispose()


async def _insert_run(
    factory,
    *,
    run_date,
    phase,
    status="done",
    phase_dry_run=False,
    created_at=None,
    error_message=None,
    model="sonnet",
):
    cat = created_at or datetime.now(tz=UTC)
    async with factory() as session:
        await session.execute(
            sa.insert(_dream_runs).values(
                run_date=run_date,
                phase=phase,
                model=model,
                status=status,
                duration_s=10.0,
                phase_dry_run=phase_dry_run,
                error_message=error_message,
                created_at=cat,
            )
        )
        await session.commit()


class TestKillswitchState:
    @pytest.mark.asyncio
    async def test_promote_wet_reorg_dry(self, session_factory, tmp_path):
        today = date.today()
        await _insert_run(session_factory, run_date=today, phase="promote", phase_dry_run=False)
        await _insert_run(session_factory, run_date=today, phase="reorg", phase_dry_run=True)

        svc = DreamRunService(session_factory, table=_dream_runs)
        # Pas de fichier killswitch → fallback sur la présence dans le run.
        state = await svc.killswitch_state(killswitches_path=tmp_path / "absent.conf")
        assert state.promote_enabled is True
        assert state.promote_dry is False
        assert state.reorg_enabled is True
        assert state.reorg_dry is True
        assert state.last_run_date == today

    @pytest.mark.asyncio
    async def test_no_activity_in_7d(self, session_factory):
        svc = DreamRunService(session_factory, table=_dream_runs)
        state = await svc.killswitch_state()
        assert state.last_run_date is None
        assert state.promote_enabled is False
        assert state.reorg_enabled is False

    @pytest.mark.asyncio
    async def test_clean_dry_nights_counter_increments(self, session_factory, tmp_path):
        for i in range(3):
            d = date.today() - timedelta(days=i)
            await _insert_run(
                session_factory, run_date=d, phase="reorg", status="done", phase_dry_run=True
            )
        svc = DreamRunService(session_factory, table=_dream_runs)
        state = await svc.killswitch_state(killswitches_path=tmp_path / "absent.conf")
        assert state.reorg_clean_dry_nights == 3

    @pytest.mark.asyncio
    async def test_model_change_resets_counter(self, session_factory, tmp_path):
        """Les nuits propres d'un AUTRE modèle ne sont pas une preuve pour celui-ci.

        2026-08-05 : le briefing annonçait « 22 clean DRY nights » pour
        ROADMAP, dont 10 produites par le secours 8B après la mort du
        primaire 80B. Ce compteur servait d'argument pour basculer en WET.
        """
        for i in (4, 3, 2):
            await _insert_run(
                session_factory,
                run_date=date.today() - timedelta(days=i),
                phase="reorg",
                phase_dry_run=True,
                model="ancien-modele",
            )
        for i in (1, 0):
            await _insert_run(
                session_factory,
                run_date=date.today() - timedelta(days=i),
                phase="reorg",
                phase_dry_run=True,
                model="nouveau-modele",
            )

        svc = DreamRunService(session_factory, table=_dream_runs)
        state = await svc.killswitch_state(killswitches_path=tmp_path / "absent.conf")
        assert state.reorg_clean_dry_nights == 2

    @pytest.mark.asyncio
    async def test_unrecorded_model_does_not_reset_a_stable_streak(self, session_factory, tmp_path):
        """Un modèle jamais renseigné reste une valeur constante, pas un changement."""
        for i in (2, 1, 0):
            await _insert_run(
                session_factory,
                run_date=date.today() - timedelta(days=i),
                phase="reorg",
                phase_dry_run=True,
                model=None,
            )

        svc = DreamRunService(session_factory, table=_dream_runs)
        state = await svc.killswitch_state(killswitches_path=tmp_path / "absent.conf")
        assert state.reorg_clean_dry_nights == 3

    @pytest.mark.asyncio
    async def test_a_degraded_night_resets_the_counter(self, session_factory, tmp_path):
        """Une nuit servie par le modèle de SECOURS n'est pas une nuit propre.

        Mesuré le 2026-08-11 : les nuits roadmap des 08-08, 08-09 et 08-10
        portent toutes status='done' ET un error_message « DÉGRADÉ : 10/10
        batches servis par le modèle de SECOURS ». Le compteur les comptait
        comme propres, et c'est ce compteur qui sert d'argument pour basculer
        une phase en WET.
        """
        await _insert_run(
            session_factory,
            run_date=date.today() - timedelta(days=1),
            phase="reorg",
            status="done",
            phase_dry_run=True,
            error_message=_DEGRADED_MESSAGE,
        )
        await _insert_run(
            session_factory,
            run_date=date.today(),
            phase="reorg",
            status="done",
            phase_dry_run=True,
        )

        svc = DreamRunService(session_factory, table=_dream_runs)
        state = await svc.killswitch_state(killswitches_path=tmp_path / "absent.conf")
        assert state.reorg_clean_dry_nights == 1

    @pytest.mark.asyncio
    async def test_an_informative_message_is_not_a_degradation(self, session_factory, tmp_path):
        """Le prédicat porte sur le PRÉFIXE, jamais sur « il y a un message ».

        `extract` écrit légitimement « N ticket(s) deferred… » sur un run
        'done'. Un prédicat `error_message IS NOT NULL` remettrait ce compteur
        à zéro sur une nuit parfaitement propre — et il passerait tous les
        autres tests de ce fichier.
        """
        for i in (2, 1, 0):
            await _insert_run(
                session_factory,
                run_date=date.today() - timedelta(days=i),
                phase="reorg",
                status="done",
                phase_dry_run=True,
                error_message=_INFORMATIVE_MESSAGE,
            )

        svc = DreamRunService(session_factory, table=_dream_runs)
        state = await svc.killswitch_state(killswitches_path=tmp_path / "absent.conf")
        assert state.reorg_clean_dry_nights == 3

    @pytest.mark.asyncio
    async def test_failure_resets_counter(self, session_factory, tmp_path):
        # dream.sh emits done|timeout|fail (NOT 'failed'); seed with 'fail'.
        await _insert_run(
            session_factory,
            run_date=date.today() - timedelta(days=3),
            phase="reorg",
            status="fail",
            phase_dry_run=True,
        )
        for i in range(2):
            d = date.today() - timedelta(days=i)
            await _insert_run(
                session_factory, run_date=d, phase="reorg", status="done", phase_dry_run=True
            )
        svc = DreamRunService(session_factory, table=_dream_runs)
        state = await svc.killswitch_state(killswitches_path=tmp_path / "absent.conf")
        assert state.reorg_clean_dry_nights == 2

    @pytest.mark.asyncio
    async def test_wet_run_resets_counter(self, session_factory, tmp_path):
        await _insert_run(
            session_factory,
            run_date=date.today() - timedelta(days=3),
            phase="reorg",
            status="done",
            phase_dry_run=False,
        )
        for i in range(2):
            d = date.today() - timedelta(days=i)
            await _insert_run(
                session_factory, run_date=d, phase="reorg", status="done", phase_dry_run=True
            )
        svc = DreamRunService(session_factory, table=_dream_runs)
        state = await svc.killswitch_state(killswitches_path=tmp_path / "absent.conf")
        assert state.reorg_clean_dry_nights == 2

    @pytest.mark.asyncio
    async def test_promote_ran_reorg_did_not(self, session_factory, tmp_path):
        # Fallback (fichier killswitch absent) : enabled = présence dans le run.
        today = date.today()
        await _insert_run(session_factory, run_date=today, phase="promote", phase_dry_run=False)
        svc = DreamRunService(session_factory, table=_dream_runs)
        state = await svc.killswitch_state(killswitches_path=tmp_path / "absent.conf")
        assert state.promote_enabled is True
        assert state.reorg_enabled is False
        assert state.reorg_dry is False
        assert state.reorg_clean_dry_nights == 0

    @pytest.mark.asyncio
    async def test_roadmap_enabled_dry_with_streak(self, session_factory, tmp_path):
        for i in range(2):
            d = date.today() - timedelta(days=i)
            await _insert_run(
                session_factory, run_date=d, phase="roadmap", status="done", phase_dry_run=True
            )
        svc = DreamRunService(session_factory, table=_dream_runs)
        state = await svc.killswitch_state(killswitches_path=tmp_path / "absent.conf")
        assert state.roadmap_enabled is True
        assert state.roadmap_dry is True
        assert state.roadmap_clean_dry_nights == 2

    @pytest.mark.asyncio
    async def test_roadmap_disabled_when_phase_absent(self, session_factory, tmp_path):
        # Fallback (fichier killswitch absent) : phase absente du run → disabled.
        await _insert_run(session_factory, run_date=date.today(), phase="promote")
        svc = DreamRunService(session_factory, table=_dream_runs)
        state = await svc.killswitch_state(killswitches_path=tmp_path / "absent.conf")
        assert state.roadmap_enabled is False
        assert state.roadmap_dry is True
        assert state.roadmap_clean_dry_nights == 0

    @pytest.mark.asyncio
    async def test_streak_counts_distinct_nights_not_rows(self, session_factory, tmp_path):
        """Un re-run manuel le même jour ne doit PAS gonfler le streak (finding 2026-07-04)."""
        d = date.today()
        for _ in range(2):  # nightly + run manuel le même jour
            await _insert_run(
                session_factory, run_date=d, phase="roadmap", status="done", phase_dry_run=True
            )
        svc = DreamRunService(session_factory, table=_dream_runs)
        state = await svc.killswitch_state(killswitches_path=tmp_path / "absent.conf")
        assert state.roadmap_clean_dry_nights == 1

    @pytest.mark.asyncio
    async def test_enabled_from_killswitch_file_when_preflight_skipped(
        self, session_factory, tmp_path
    ):
        """Fix faux positif (learning 6d5ee46b) : le pre-flight gate skippe
        promote/reorg quand le corpus est inchangé → ABSENTS du dernier run
        MAIS toujours enabled. L'état enabled vient du FICHIER killswitch, pas
        de la présence dans dream_runs. Le mode dry d'une phase skippée retombe
        sur le DRY_RUN déclaré du fichier."""
        today = date.today()
        # Nuit pre-flight-skippée : seules les phases non-Opus tournent.
        await _insert_run(session_factory, run_date=today, phase="extract", phase_dry_run=True)
        await _insert_run(session_factory, run_date=today, phase="roadmap", phase_dry_run=False)
        ks = tmp_path / "killswitches.conf"
        ks.write_text(
            "[Service]\n"
            "Environment=BRAIN_DREAM_PROMOTE_ENABLED=true\n"
            "Environment=BRAIN_DREAM_REORG_ENABLED=true\n"
            "Environment=BRAIN_DREAM_REORG_DRY_RUN=true\n"
        )
        svc = DreamRunService(session_factory, table=_dream_runs)
        state = await svc.killswitch_state(killswitches_path=ks)
        assert state.promote_enabled is True  # depuis le fichier, malgré absence du run
        assert state.reorg_enabled is True
        assert state.reorg_dry is True  # phase absente → fallback DRY_RUN fichier
        assert state.last_run_date == today

    @pytest.mark.asyncio
    async def test_file_disabled_is_source_of_truth(self, session_factory, tmp_path):
        """Le fichier est la source de vérité : REORG=false → disabled même si
        une row reorg existe (edge : killswitch posé après un run)."""
        await _insert_run(session_factory, run_date=date.today(), phase="reorg", phase_dry_run=True)
        ks = tmp_path / "killswitches.conf"
        ks.write_text("[Service]\nEnvironment=BRAIN_DREAM_REORG_ENABLED=false\n")
        svc = DreamRunService(session_factory, table=_dream_runs)
        state = await svc.killswitch_state(killswitches_path=ks)
        assert state.reorg_enabled is False

    @pytest.mark.asyncio
    async def test_sweep_enabled_and_dry_from_the_drop_in(self, session_factory, tmp_path):
        """SWEEP armé et déclaré dry par le drop-in, phase absente du dernier run."""
        await _insert_run(session_factory, run_date=date.today(), phase="promote")
        ks = tmp_path / "killswitches.conf"
        ks.write_text(
            "[Service]\n"
            "Environment=BRAIN_DREAM_SWEEP_ENABLED=true\n"
            "Environment=BRAIN_DREAM_SWEEP_DRY_RUN=true\n"
        )

        svc = DreamRunService(session_factory, table=_dream_runs)
        state = await svc.killswitch_state(killswitches_path=ks)

        assert state.sweep_enabled is True
        assert state.sweep_dry is True

    @pytest.mark.asyncio
    async def test_sweep_dry_is_false_when_the_drop_in_declares_wet(
        self, session_factory, tmp_path
    ):
        """Un `sweep_dry = True` en dur passerait tous les autres tests : celui-ci le tue."""
        await _insert_run(session_factory, run_date=date.today(), phase="promote")
        ks = tmp_path / "killswitches.conf"
        ks.write_text(
            "[Service]\n"
            "Environment=BRAIN_DREAM_SWEEP_ENABLED=true\n"
            "Environment=BRAIN_DREAM_SWEEP_DRY_RUN=false\n"
        )

        svc = DreamRunService(session_factory, table=_dream_runs)
        state = await svc.killswitch_state(killswitches_path=ks)

        assert state.sweep_dry is False

    @pytest.mark.asyncio
    async def test_sweep_dry_defaults_to_true_when_the_key_is_absent(
        self, session_factory, tmp_path
    ):
        """Clé DRY_RUN absente → défaut conservateur dry, comme extract et roadmap."""
        await _insert_run(session_factory, run_date=date.today(), phase="promote")
        ks = tmp_path / "killswitches.conf"
        ks.write_text("[Service]\nEnvironment=BRAIN_DREAM_SWEEP_ENABLED=true\n")

        svc = DreamRunService(session_factory, table=_dream_runs)
        state = await svc.killswitch_state(killswitches_path=ks)

        assert state.sweep_dry is True

    @pytest.mark.asyncio
    async def test_sweep_disabled_when_the_key_is_absent_from_the_drop_in(
        self, session_factory, tmp_path
    ):
        await _insert_run(session_factory, run_date=date.today(), phase="promote")
        ks = tmp_path / "killswitches.conf"
        ks.write_text("[Service]\nEnvironment=BRAIN_DREAM_PROMOTE_ENABLED=true\n")

        svc = DreamRunService(session_factory, table=_dream_runs)
        state = await svc.killswitch_state(killswitches_path=ks)

        assert state.sweep_enabled is False

    @pytest.mark.asyncio
    async def test_sweep_disabled_when_the_drop_in_says_false_despite_a_row(
        self, session_factory, tmp_path
    ):
        """Le fichier reste la source de vérité même si la phase a tourné."""
        await _insert_run(
            session_factory, run_date=date.today(), phase="sweep", phase_dry_run=True, model=None
        )
        ks = tmp_path / "killswitches.conf"
        ks.write_text("[Service]\nEnvironment=BRAIN_DREAM_SWEEP_ENABLED=false\n")

        svc = DreamRunService(session_factory, table=_dream_runs)
        state = await svc.killswitch_state(killswitches_path=ks)

        assert state.sweep_enabled is False

    @pytest.mark.asyncio
    async def test_sweep_clean_dry_nights_counts_the_real_streak(self, session_factory, tmp_path):
        """Un constant 0 passerait un test à streak nulle : on exige un compte exact non nul."""
        for i in range(3):
            await _insert_run(
                session_factory,
                run_date=date.today() - timedelta(days=i),
                phase="sweep",
                status="done",
                phase_dry_run=True,
                model=None,
            )
        ks = tmp_path / "killswitches.conf"
        ks.write_text("[Service]\nEnvironment=BRAIN_DREAM_SWEEP_ENABLED=true\n")

        svc = DreamRunService(session_factory, table=_dream_runs)
        state = await svc.killswitch_state(killswitches_path=ks)

        assert state.sweep_clean_dry_nights == 3


class TestLastFailure:
    @pytest.mark.asyncio
    async def test_returns_most_recent_failure(self, session_factory):
        old_failure = datetime.now(tz=UTC) - timedelta(days=2)
        new_failure = datetime.now(tz=UTC) - timedelta(hours=4)
        await _insert_run(
            session_factory,
            run_date=date.today() - timedelta(days=2),
            phase="promote",
            status="fail",
            created_at=old_failure,
        )
        await _insert_run(
            session_factory,
            run_date=date.today(),
            phase="reorg",
            status="fail",
            created_at=new_failure,
        )
        svc = DreamRunService(session_factory, table=_dream_runs)
        result = await svc.last_failure()
        assert result is not None
        assert result.phase == "reorg"

    @pytest.mark.asyncio
    async def test_returns_none_when_no_failures(self, session_factory):
        await _insert_run(session_factory, run_date=date.today(), phase="promote", status="done")
        svc = DreamRunService(session_factory, table=_dream_runs)
        assert await svc.last_failure() is None

    @pytest.mark.asyncio
    async def test_filters_outside_window(self, session_factory):
        old = datetime.now(tz=UTC) - timedelta(days=14)
        await _insert_run(
            session_factory,
            run_date=date.today() - timedelta(days=14),
            phase="reorg",
            status="fail",
            created_at=old,
        )
        svc = DreamRunService(session_factory, table=_dream_runs)
        result = await svc.last_failure(within_days=7)
        assert result is None
