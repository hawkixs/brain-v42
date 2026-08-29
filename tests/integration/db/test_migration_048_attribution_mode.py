"""Migration 048 — la base sait dire PAR QUELLE CLÉ une ligne a été attribuée.

Sans ce rail, la 048 serait une intention. Trois choses ne se prouvent qu'ici :
le CHECK refuse un mode inventé, l'index partiel existe pour de vrai, et surtout
le downgrade REFUSE de détruire ce qui distingue une preuve d'une déduction.

Ce dernier point est le seul qui compte vraiment. Le downgrade ne perd aucune
ligne de ledger : les artefacts gardent leur session, et une base downgradée a
l'air parfaitement saine. Ce qu'elle a perdu — « cette attribution a été DEVINÉE,
pas prouvée » — est invisible dans les données restantes. C'est exactement la
classe de perte qu'un refus doit NOMMER, parce que personne ne la remarquera.
"""

from __future__ import annotations

import os
import subprocess
import sys
from pathlib import Path
from uuid import uuid4

import pytest
import sqlalchemy as sa
from sqlalchemy.exc import IntegrityError
from sqlalchemy.ext.asyncio import AsyncEngine

pytestmark = pytest.mark.integration

_PROJECT_ROOT = Path(__file__).resolve().parents[3]


async def _session_with_one_artifact(engine: AsyncEngine, *, mode: str | None) -> tuple[str, str]:
    """Un projet, une session, un artefact de ledger portant ce mode."""
    project_key = f"integ-048-{uuid4().hex[:10]}"
    knowledge_id = str(uuid4())
    async with engine.begin() as conn:
        await conn.execute(
            sa.text(
                "INSERT INTO project_contexts (project_key, name, description) "
                "VALUES (:project_key, :project_key, 'migration 048 scene')"
            ),
            {"project_key": project_key},
        )
        session_id = await conn.scalar(
            sa.text(
                "INSERT INTO brain_sessions "
                "(id, project_key, client_key, status, started_focus_revision) "
                "VALUES (gen_random_uuid(), :project_key, :client_key, 'open', 0) "
                "RETURNING id"
            ),
            {"project_key": project_key, "client_key": f"c-{uuid4().hex[:8]}"},
        )
        await conn.execute(
            sa.text(
                "INSERT INTO brain_session_artifacts "
                "(knowledge_id, session_id, knowledge_type, attribution_mode) "
                "VALUES (CAST(:knowledge_id AS uuid), :session_id, 'learning', :mode)"
            ),
            {"knowledge_id": knowledge_id, "session_id": session_id, "mode": mode},
        )
    return project_key, knowledge_id


async def _cleanup(engine: AsyncEngine, project_key: str) -> None:
    async with engine.begin() as conn:
        await conn.execute(
            sa.text(
                "DELETE FROM brain_session_artifacts WHERE session_id IN "
                "(SELECT id FROM brain_sessions WHERE project_key = :project_key)"
            ),
            {"project_key": project_key},
        )
        await conn.execute(
            sa.text("DELETE FROM brain_sessions WHERE project_key = :project_key"),
            {"project_key": project_key},
        )
        await conn.execute(
            sa.text("DELETE FROM project_contexts WHERE project_key = :project_key"),
            {"project_key": project_key},
        )


async def test_the_check_accepts_the_four_modes_and_nothing_else(engine: AsyncEngine) -> None:
    """Quatre modes nommés, et un cinquième inventé échoue à l'INSERT.

    Le CHECK est ce qui fait échouer une faute de frappe ici plutôt qu'en
    production, six mois plus tard, sur une ligne qu'on ne saura plus relire.
    """
    for mode in ("explicit", "derived_deposit", "derived_connection", "derived_window", None):
        project_key, _ = await _session_with_one_artifact(engine, mode=mode)
        await _cleanup(engine, project_key)

    with pytest.raises(IntegrityError, match="attribution_mode_valid"):
        project_key, _ = await _session_with_one_artifact(engine, mode="derived_vibes")
    # L'INSERT a échoué : rien à nettoyer, la transaction est morte avec lui.


async def test_the_deduced_mode_is_the_only_one_with_its_own_index(engine: AsyncEngine) -> None:
    """Défaire une devinette doit être une REQUÊTE, pas un scan.

    Les trois autres modes ne se cherchent jamais en masse : `explicit` est le
    cas normal, et les deux dérivés se lisent ligne à ligne. Indexer les quatre
    coûterait quatre index pour une seule question réelle.
    """
    async with engine.connect() as conn:
        indexes = set(
            (
                await conn.execute(
                    sa.text(
                        "SELECT indexname FROM pg_indexes "
                        "WHERE tablename = 'brain_session_artifacts'"
                    )
                )
            )
            .scalars()
            .all()
        )
        predicate = await conn.scalar(
            sa.text(
                "SELECT indexdef FROM pg_indexes "
                "WHERE indexname = 'idx_brain_session_artifacts_derived_window'"
            )
        )

    assert "idx_brain_session_artifacts_derived_window" in indexes
    assert predicate is not None and "derived_window" in predicate
    assert "WHERE" in predicate, "un index PLEIN coûterait pour trois modes qu'on ne cherche pas"


async def test_the_downgrade_refuses_to_erase_what_marks_a_guess_as_a_guess(
    engine: AsyncEngine,
    migration_downgrade_fence,
) -> None:
    """Fail-closed, gabarit 047 : compter ET NOMMER.

    Un downgrade muet réussirait ici — aucune contrainte ne s'y oppose, la
    colonne disparaît et tout paraît sain. C'est précisément pour ça qu'il faut
    un refus explicite : la perte n'a aucun symptôme.
    """
    project_key, knowledge_id = await _session_with_one_artifact(engine, mode="derived_window")
    migration_downgrade_fence("047")

    from tests.integration.conftest import INTEGRATION_DB_URL

    async with engine.connect() as conn:
        head_before = await conn.scalar(sa.text("SELECT version_num FROM alembic_version"))

    try:
        result = subprocess.run(
            [sys.executable, "-m", "alembic", "downgrade", "047"],
            env={**os.environ, "POSTGRES_URL": INTEGRATION_DB_URL},
            cwd=str(_PROJECT_ROOT),
            capture_output=True,
            text=True,
            timeout=120,
        )

        assert result.returncode != 0, "le downgrade a effacé une déduction en silence"
        assert "cannot downgrade 048" in result.stderr
        # NOMMER, pas seulement compter : un message qui dit « 1 ligne » sans
        # dire laquelle laisse l'opérateur sans geste possible.
        assert knowledge_id in result.stderr
        assert "allow_attribution_mode_downgrade=yes" in result.stderr

        async with engine.connect() as conn:
            head = await conn.scalar(sa.text("SELECT version_num FROM alembic_version"))
        # « Intacte » = LÀ OÙ ELLE ÉTAIT, pas un littéral : la descente
        # multi-étapes roule dans une transaction — depuis la 049, le refus de
        # la 048 annule aussi l'étape 049→048, et un littéral « 048 » aurait
        # rougi à chaque nouvelle tête pour une raison sans rapport.
        assert head == head_before, "le refus doit laisser la tête intacte"
    finally:
        await _cleanup(engine, project_key)


async def test_the_named_opt_in_lets_a_deliberate_operator_through(
    engine: AsyncEngine,
    migration_downgrade_fence,
) -> None:
    """Le refus est une garde, pas une porte murée — et l'opt-in est NOMMÉ.

    Un drapeau générique se recopie d'une migration à l'autre sans qu'on relise
    ce qu'il autorise ; celui-ci ne veut dire qu'une chose et ne s'applique qu'à
    la 048.
    """
    project_key, _ = await _session_with_one_artifact(engine, mode="derived_window")
    migration_downgrade_fence("047")

    from tests.integration.conftest import INTEGRATION_DB_URL

    try:
        result = subprocess.run(
            [
                sys.executable,
                "-m",
                "alembic",
                "-x",
                "allow_attribution_mode_downgrade=yes",
                "downgrade",
                "047",
            ],
            env={**os.environ, "POSTGRES_URL": INTEGRATION_DB_URL},
            cwd=str(_PROJECT_ROOT),
            capture_output=True,
            text=True,
            timeout=120,
        )
        assert result.returncode == 0, result.stderr

        async with engine.connect() as conn:
            head = await conn.scalar(sa.text("SELECT version_num FROM alembic_version"))
            columns = set(
                (
                    await conn.execute(
                        sa.text(
                            "SELECT column_name FROM information_schema.columns "
                            "WHERE table_name = 'brain_session_artifacts'"
                        )
                    )
                )
                .scalars()
                .all()
            )
        assert head == "047"
        assert "attribution_mode" not in columns
        # La ligne de ledger, elle, est TOUJOURS là : ce downgrade ne perd pas
        # d'attribution, il perd la trace de ce qui la distinguait d'une preuve.
        async with engine.connect() as conn:
            surviving = await conn.scalar(
                sa.text(
                    "SELECT count(*) FROM brain_session_artifacts a "
                    "JOIN brain_sessions s ON s.id = a.session_id "
                    "WHERE s.project_key = :project_key"
                ),
                {"project_key": project_key},
            )
        assert surviving == 1
    finally:
        # REMONTER SOI-MÊME. Le filet du conftest existe pour le cas où une
        # assertion rouge court-circuite ce bloc, pas pour tenir lieu de
        # restauration : un test qui passe et laisse la base derrière lui
        # empoisonne tous les suivants, et le filet le dit explicitement.
        subprocess.run(
            [sys.executable, "-m", "alembic", "upgrade", "head"],
            env={**os.environ, "POSTGRES_URL": INTEGRATION_DB_URL},
            cwd=str(_PROJECT_ROOT),
            capture_output=True,
            text=True,
            timeout=120,
            check=True,
        )
        await _cleanup(engine, project_key)
