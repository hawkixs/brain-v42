"""Le générateur de clés des tests unitaires et le prédicat de purge doivent s'accorder.

Ces deux règles vivent dans des modules indépendants — `tests/unit/keys.py` et
`_INTEGRATION_PROJECT_PREDICATE` dans `tests/integration/conftest.py` — et rien
ne les obligeait à parler de la même chose. Elles ont divergé en silence :
`tests/unit/` frappe la même base `brain_test`, ses clés portaient des préfixes
inventés sur place (`t8-`, `t9-`, `rv-`, `t-adr-`, `t-run-`), et le prédicat ne
connaît que `integ-`. Mesuré le 2026-08-11 dans brain_test : 5 674 learnings,
dont 4 241 sous `t8-` et 581 sous `t9-`, contre 188 lignes réelles.

Le défaut est invisible en CI, qui recrée sa base à chaque pipeline. Il ne se
manifeste que sur les bases de développement locales — l'endroit où il diverge
par machine et où personne ne le regarde (ticket cb888186).

Ce test est le point de contact : il fabrique une clé par le chemin que les
tests unitaires empruntent, puis lui applique la fonction de purge RÉELLE. Il
échoue si l'un des deux bouts bouge sans l'autre.
"""

from __future__ import annotations

import pytest
import pytest_asyncio
import sqlalchemy as sa
from sqlalchemy.ext.asyncio import AsyncEngine, create_async_engine
from sqlalchemy.pool import NullPool

from brain_v42.db.tables import learnings
from tests.conftest import require_test_db_url
from tests.integration.conftest import purge_integration_rows
from tests.unit.keys import make_unit_project_key


@pytest_asyncio.fixture(scope="module")
async def _engine() -> AsyncEngine:  # type: ignore[misc]
    eng = create_async_engine(require_test_db_url(), poolclass=NullPool, echo=False)
    try:
        async with eng.connect() as conn:
            await conn.execute(sa.text("SELECT 1"))
    except Exception as exc:  # noqa: BLE001
        await eng.dispose()
        pytest.skip(f"PostgreSQL test database is not reachable: {exc}")
    yield eng
    await eng.dispose()


async def test_a_key_built_for_a_unit_test_is_deleted_by_the_real_purge(
    _engine: AsyncEngine,
) -> None:
    """Le contrat qui manquait : ce que les tests unitaires écrivent, la purge l'efface."""
    key = make_unit_project_key("purge-probe")

    async with _engine.begin() as conn:
        await conn.execute(
            sa.insert(learnings).values(
                topic="purge probe",
                insight="written by test_unit_project_keys_are_purged",
                project_key=key,
                source_type="experience",
                confidence="low",
            )
        )

    async with _engine.connect() as conn:
        seeded = await conn.scalar(
            sa.select(sa.func.count()).select_from(learnings).where(learnings.c.project_key == key)
        )
    # Sans cette assertion, une insertion silencieusement perdue rendrait le
    # test vert sur du vide — la purge n'aurait alors rien eu à effacer.
    assert seeded == 1, (
        "la ligne témoin n'a pas été écrite, le test suivant serait vert sur du vide"
    )

    async with _engine.begin() as conn:
        await purge_integration_rows(conn)

    async with _engine.connect() as conn:
        remaining = await conn.scalar(
            sa.select(sa.func.count()).select_from(learnings).where(learnings.c.project_key == key)
        )
    assert remaining == 0, (
        f"la clé {key!r} a survécu à la purge : le préfixe des tests unitaires "
        "n'est pas couvert par _INTEGRATION_PROJECT_PREDICATE"
    )
