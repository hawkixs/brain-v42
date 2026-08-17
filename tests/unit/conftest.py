"""Fixtures partagées des tests unitaires."""

from __future__ import annotations

import os
from collections.abc import Iterator

import pytest
import pytest_asyncio


@pytest_asyncio.fixture(scope="session", autouse=True)
async def _purge_unit_rows() -> None:  # type: ignore[misc]
    """Nettoyer, une fois la session unitaire finie, ce qu'elle a écrit en base.

    ``tests/unit/`` frappe la MÊME base ``brain_test`` que la suite
    d'intégration dès que ``BRAIN_V42_TEST_DB_URL`` est posée, et les deux rails
    CI la posent. Mais ``cleanup_test_data`` est une fixture de
    ``tests/integration/conftest.py`` : elle ne s'applique qu'à SA suite. Sept
    modules unitaires écrivaient donc sans que rien ne les efface — 5 674
    learnings mesurés dans brain_test le 2026-08-11 pour 188 lignes réelles
    (ticket cb888186).

    Le symptôme est invisible en CI, qui recrée sa base à chaque pipeline ; il ne
    grossit que sur les bases de développement locales, donc différemment sur
    chaque machine. C'est le pire endroit pour un défaut.

    Silencieuse quand la variable est absente : la majorité des tests unitaires
    ne touchent aucune base et ne doivent pas payer une connexion pour ça.
    """
    yield  # type: ignore[misc]

    url = os.environ.get("BRAIN_V42_TEST_DB_URL")
    if not url or not url.strip():
        return

    import sqlalchemy as sa
    from sqlalchemy.ext.asyncio import create_async_engine
    from sqlalchemy.pool import NullPool

    # Importé du conftest d'intégration à dessein : DEUX purges divergeraient, et
    # la divergence ne se verrait que le jour où l'une des deux laisse passer
    # quelque chose. `tests` est un package et ce croisement est déjà un motif du
    # dépôt (tests/integration/test_cleanup_purge_scope.py).
    from tests.integration.conftest import purge_integration_rows

    engine = create_async_engine(url, poolclass=NullPool, echo=False)
    try:
        async with engine.connect() as conn:
            await conn.execute(sa.text("SELECT 1"))
    except Exception:  # noqa: BLE001 — base injoignable : rien à nettoyer, rien à signaler
        await engine.dispose()
        return
    try:
        async with engine.begin() as conn:
            await purge_integration_rows(conn)
    finally:
        await engine.dispose()


@pytest.fixture(autouse=True)
def _reset_activity_reporter() -> Iterator[None]:
    """Remettre l'émetteur d'activité global à ``None`` autour de chaque test.

    ``brain_v42.mcp.activity_reporter._reporter`` est un global de processus,
    construit paresseusement au premier appel de tool. Deux modules de tests y
    injectent un double — l'émetteur lui-même et le câblage du middleware de
    provenance — et un double laissé en place fuiterait sur tous les tests
    suivants du processus. La remise à zéro vit ici plutôt que dupliquée dans
    chaque module : elle protège aussi les tests qui traversent le middleware
    sans savoir qu'ils touchent ce global.
    """
    from brain_v42.mcp import activity_reporter

    activity_reporter.set_activity_reporter(None)
    yield
    activity_reporter.set_activity_reporter(None)
