"""Migration 042 — dream_runs sait enfin dire pour quel projet une nuit a tourné.

Le défaut que cette colonne répare est mesuré : `dream_runs` couvre huit phases
sur quatre mois et aucune ligne ne dit à quel projet elle appartient, donc toute
cette télémétrie est indistinguable. Le schéma lui-même ne pouvait pas exprimer
la dimension qu'on croyait couverte. (Aucun décompte de lignes ni de nuits n'est
épinglé ici : la table en gagne six à neuf chaque matin à 06:00, et un test qui
fige un tel chiffre rougit tout seul le lendemain.)

Ce que ces tests fixent, et que le seul `add_column` ne dirait pas :
  1. La colonne est NULLABLE, et c'est une conséquence — aucun des SIX écrivains
     ne fait remonter son échec (spec §15.3). NOT NULL transformerait une erreur
     de schéma en avertissement imprimé sur tous ceux qui tournent.
  2. Aucun backfill : les lignes d'avant restent NULL, définitivement.
  3. La sentinelle `'*'` des phases globales est stockable.
  4. L'index composite existe, dans le bon ordre — c'est lui qui rend la
     télémétrie par projet interrogeable sans balayer la table.
"""

from __future__ import annotations

import datetime as dt

import pytest
import sqlalchemy as sa

pytestmark = pytest.mark.integration

_INDEX = "idx_dream_runs_date_project"


class TestColumnShape:
    async def test_column_exists_and_is_nullable(self, db_session) -> None:
        row = (
            await db_session.execute(
                sa.text(
                    "SELECT data_type, is_nullable, character_maximum_length "
                    "FROM information_schema.columns "
                    "WHERE table_name = 'dream_runs' AND column_name = 'project_key'"
                )
            )
        ).one_or_none()
        assert row is not None, "la colonne project_key est absente de dream_runs"
        data_type, is_nullable, max_length = row
        assert data_type == "character varying"
        assert max_length == 64
        # Nullable n'est pas de la prudence : voir spec §14.3. Un NOT NULL ferait
        # avaler l'erreur par ticket_extract, roadmap_curate et session_sweep.
        assert is_nullable == "YES"

    async def test_has_no_default(self, db_session) -> None:
        """Un défaut 'brain-v42' étiquetterait chaque nuit du mauvais projet."""
        default = (
            await db_session.execute(
                sa.text(
                    "SELECT column_default FROM information_schema.columns "
                    "WHERE table_name = 'dream_runs' AND column_name = 'project_key'"
                )
            )
        ).scalar_one()
        assert default is None


class TestNoBackfill:
    async def test_a_row_inserted_without_the_key_stays_null(self, db_session) -> None:
        """NULL veut dire « écrit avant la 042 », et rien d'autre."""
        run_id = (
            await db_session.execute(
                sa.text(
                    "INSERT INTO dream_runs (run_date, phase, status) "
                    "VALUES (:d, 'scan', 'done') RETURNING id"
                ),
                {"d": dt.date(2026, 1, 1)},
            )
        ).scalar_one()
        value = (
            await db_session.execute(
                sa.text("SELECT project_key FROM dream_runs WHERE id = :id"), {"id": run_id}
            )
        ).scalar_one()
        assert value is None


class TestSentinelAndRealKeys:
    async def test_global_phase_sentinel_is_storable(self, db_session) -> None:
        """`'*'` est écrit par les trois phases globales, et par elles seules."""
        run_id = (
            await db_session.execute(
                sa.text(
                    "INSERT INTO dream_runs (run_date, phase, status, project_key) "
                    "VALUES (:d, 'extract', 'done', '*') RETURNING id"
                ),
                {"d": dt.date(2026, 1, 2)},
            )
        ).scalar_one()
        value = (
            await db_session.execute(
                sa.text("SELECT project_key FROM dream_runs WHERE id = :id"), {"id": run_id}
            )
        ).scalar_one()
        assert value == "*"

    async def test_a_long_project_key_fits(self, db_session) -> None:
        """64 caractères, pas 50 : les clés composées comme `red-lab:orchestrator`
        existent déjà et rien ne garantit qu'elles ne s'allongeront pas."""
        key = "a" * 64
        run_id = (
            await db_session.execute(
                sa.text(
                    "INSERT INTO dream_runs (run_date, phase, status, project_key) "
                    "VALUES (:d, 'scan', 'done', :k) RETURNING id"
                ),
                {"d": dt.date(2026, 1, 3), "k": key},
            )
        ).scalar_one()
        value = (
            await db_session.execute(
                sa.text("SELECT project_key FROM dream_runs WHERE id = :id"), {"id": run_id}
            )
        ).scalar_one()
        assert value == key


class TestIndex:
    async def test_composite_index_exists(self, db_session) -> None:
        definition = (
            await db_session.execute(
                sa.text("SELECT indexdef FROM pg_indexes WHERE indexname = :n"),
                {"n": _INDEX},
            )
        ).scalar_one_or_none()
        assert definition is not None, f"index {_INDEX} absent"
        assert "run_date DESC" in definition
        assert "project_key" in definition

    async def test_the_pre_042_date_index_is_left_alone(self, db_session) -> None:
        """Le nouvel index s'ajoute, il ne remplace pas.

        `idx_dream_runs_date(run_date DESC)` sert les lectures qui ignorent le
        projet — les lecteurs recensés en §15.6 sont tous dans ce cas
        aujourd'hui. Le retirer casserait leur plan pour un gain nul.
        """
        found = (
            await db_session.execute(
                sa.text("SELECT indexname FROM pg_indexes WHERE indexname = 'idx_dream_runs_date'")
            )
        ).scalar_one_or_none()
        assert found == "idx_dream_runs_date"
