"""Migration 047 — la base cesse d'exiger un reçu de capture pour fermer.

Le troisième rail de la scène. Le répertoire et le modèle Pydantic ont laissé
passer une fermeture dont le SERVEUR avait rempli le ledger ; c'est ici qu'on
prouve que la base aussi. Sans ce rail, la 047 serait une intention : le CHECK
`brain_sessions_terminal_state_valid` refusait la ligne, et l'utilisateur se
retrouvait avec une session qu'il ne pouvait plus fermer.

Ce qui RESTE refusé est le point : une raison blanche. Donner une raison est un
acte, et le serveur ne peut pas l'accomplir à la place de l'utilisateur.
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

_ENDED = sa.text(
    """
    INSERT INTO brain_sessions (
        id, project_key, client_key, status, started_focus, started_focus_revision,
        captured_knowledge_ids, nothing_to_capture_reason, summary, next_focus,
        ended_at, end_expected_focus_revision, focus_outcome, focus_at_end,
        focus_revision_at_end
    ) VALUES (
        gen_random_uuid(), :project_key, :client_key, 'ended', 'old', 7,
        CAST(:captured AS uuid[]), :reason, 'reviewed design', 'implement tools',
        now(), 7, 'applied', 'implement tools', 8
    )
    """
)


async def _project(engine: AsyncEngine) -> str:
    project_key = f"integ-047-{uuid4().hex[:10]}"
    async with engine.begin() as conn:
        await conn.execute(
            sa.text(
                "INSERT INTO project_contexts (project_key, name, description) "
                "VALUES (:project_key, :project_key, 'migration 047 scene')"
            ),
            {"project_key": project_key},
        )
    return project_key


async def _close(
    engine: AsyncEngine,
    project_key: str,
    *,
    client_key: str,
    captured: list[str],
    reason: str | None,
) -> None:
    async with engine.begin() as conn:
        await conn.execute(
            _ENDED,
            {
                "project_key": project_key,
                "client_key": client_key,
                "captured": captured,
                "reason": reason,
            },
        )


async def test_an_ended_session_may_hold_a_derived_ledger_and_a_reason(
    engine: AsyncEngine,
) -> None:
    """LE cas qui motive la révision : le serveur a attribué, l'utilisateur dit
    « rien de durable ». Avant la 047, cette session était infermable."""
    project_key = await _project(engine)
    await _close(
        engine,
        project_key,
        client_key="derived",
        captured=[str(uuid4())],
        reason="no durable new knowledge",
    )


async def test_an_ended_session_may_hold_neither_a_ledger_nor_a_reason(
    engine: AsyncEngine,
) -> None:
    """La porte n'est plus la diligence : ne rien avoir produit est une issue."""
    project_key = await _project(engine)
    await _close(engine, project_key, client_key="quiet", captured=[], reason=None)


async def test_a_blank_reason_is_still_refused_by_the_database(engine: AsyncEngine) -> None:
    """Ce que la 047 ne relâche PAS, et la seule chose que le serveur ne peut pas produire."""
    project_key = await _project(engine)
    with pytest.raises(IntegrityError, match="brain_sessions_terminal_state_valid"):
        await _close(engine, project_key, client_key="blank", captured=[], reason="   ")


async def test_the_downgrade_refuses_to_destroy_a_closure_it_cannot_restore(
    engine: AsyncEngine,
    migration_downgrade_fence,
) -> None:
    """Fail-closed, gabarit 037 : le downgrade NOMME ce qu'il détruirait.

    Les deux formes que la 047 rend légales sont illégales sans elle. Un
    downgrade muet échouerait quand même — la contrainte le refuserait — mais
    avec un message de contrainte, sans dire QUI est en cause. Celui-ci le dit,
    et compte.
    """
    project_key = await _project(engine)
    await _close(
        engine,
        project_key,
        client_key="derived",
        captured=[str(uuid4())],
        reason="no durable new knowledge",
    )
    migration_downgrade_fence("046")

    from tests.integration.conftest import INTEGRATION_DB_URL

    result = subprocess.run(
        [sys.executable, "-m", "alembic", "downgrade", "046"],
        env={**os.environ, "POSTGRES_URL": INTEGRATION_DB_URL},
        cwd=str(_PROJECT_ROOT),
        capture_output=True,
        text=True,
        timeout=120,
    )

    assert result.returncode != 0, "le downgrade a détruit une fermeture dérivée en silence"
    assert "cannot downgrade 047" in result.stderr
    assert "ended session(s) hold a capture outcome" in result.stderr

    async with engine.connect() as conn:
        head = await conn.scalar(sa.text("SELECT version_num FROM alembic_version"))
    assert head == "047", "le refus doit laisser la tête intacte"
