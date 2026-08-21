"""Instantané des tags AVANT la phase REORG — le seul « avant » qui soit observé.

Le validateur ne peut pas prouver qu'une entité a bougé s'il ne connaît que son
état d'après. `updated_at` semblait tenir ce rôle et ne le tenait pas : le
`DecayFlusher` émet un `UPDATE` en masse sur `learnings` et `decisions` toutes
les 300 s, et le trigger `update_updated_at()` de la migration 001 est
INCONDITIONNEL — il n'a pas de clause `WHEN`. Pire, ce sont les lectures de
REORG lui-même (`brain_get` avant chaque normalisation) qui alimentent
l'access_log dont le flusher se sert : la phase fabriquait donc sa propre preuve.

`content_updated_at` (migration 041) ne peut pas le remplacer non plus : ses
triggers sont déclarés `BEFORE UPDATE OF topic, insight` sur `learnings` et
`BEFORE UPDATE OF title, description, reasoning, consequences` sur `decisions`.
`tags` n'y figure pas, et REORG ne mute QUE des tags — la colonne reste donc
`NULL` sur exactement les entités qu'on cherche à vérifier.

Reste l'instantané : lire les tags du corpus du projet juste avant la phase, et
comparer après. C'est la seule forme où le « avant » est mesuré et non déduit.
"""

from __future__ import annotations

import asyncio
import json
import uuid

import pytest
import pytest_asyncio
import sqlalchemy as sa
from scripts.dream.reorg_snapshot import snapshot_tags
from sqlalchemy.ext.asyncio import (
    AsyncEngine,
    AsyncSession,
    async_sessionmaker,
    create_async_engine,
)
from sqlalchemy.pool import NullPool

from tests.conftest import require_test_db_url
from tests.unit.keys import make_unit_project_key


@pytest_asyncio.fixture(scope="module")
async def _engine() -> AsyncEngine:  # type: ignore[misc]
    eng = create_async_engine(require_test_db_url(), poolclass=NullPool, echo=False)
    try:
        async with eng.connect() as conn:
            await conn.execute(sa.text("SELECT 1"))
    except Exception as exc:  # noqa: BLE001
        pytest.skip(f"PostgreSQL not reachable: {exc}")
    yield eng  # type: ignore[misc]
    await eng.dispose()


@pytest_asyncio.fixture
async def session_factory(_engine: AsyncEngine) -> async_sessionmaker[AsyncSession]:
    return async_sessionmaker(_engine, class_=AsyncSession, expire_on_commit=False)


async def _seed_learning(
    session_factory: async_sessionmaker[AsyncSession],
    project_key: str,
    tags: list[str],
) -> uuid.UUID:
    from brain_v42.db.tables import learnings

    async with session_factory() as session:
        async with session.begin():
            return (
                await session.execute(
                    learnings.insert()
                    .values(
                        topic=f"t-{uuid.uuid4().hex[:6]}",
                        insight="i",
                        project_key=project_key,
                        source_type="experience",
                        confidence="high",
                        tags=tags,
                    )
                    .returning(learnings.c.id)
                )
            ).scalar_one()


async def _seed_decision(
    session_factory: async_sessionmaker[AsyncSession],
    project_key: str,
    tags: list[str],
) -> uuid.UUID:
    from brain_v42.db.tables import decisions

    async with session_factory() as session:
        async with session.begin():
            return (
                await session.execute(
                    decisions.insert()
                    .values(
                        title=f"D-{uuid.uuid4().hex[:6]}",
                        description="d",
                        reasoning="r",
                        alternatives=[],
                        project_key=project_key,
                        tags=tags,
                    )
                    .returning(decisions.c.id)
                )
            ).scalar_one()


@pytest.mark.asyncio
async def test_the_snapshot_covers_both_tables_reorg_can_mutate(
    session_factory: async_sessionmaker[AsyncSession],
) -> None:
    """REORG ne touche que `learnings` et `decisions` — l'instantané aussi.

    Manquer une des deux tables donnerait le pire des résultats : un « absent de
    l'instantané » sur des entités parfaitement légitimes, donc une nuit rouge
    pour une raison inventée.
    """
    pk = make_unit_project_key("rs")
    lid = await _seed_learning(session_factory, pk, ["alpha"])
    did = await _seed_decision(session_factory, pk, ["beta", "gamma"])

    taken = await snapshot_tags(session_factory, pk)

    assert taken[str(lid)] == ["alpha"]
    assert taken[str(did)] == ["beta", "gamma"]


@pytest.mark.asyncio
async def test_the_snapshot_stops_at_the_project_boundary(
    session_factory: async_sessionmaker[AsyncSession],
) -> None:
    """Le périmètre de l'instantané est celui du run, pas le corpus entier.

    Un instantané global rendrait « présent dans l'instantané » vrai pour une
    entité d'un autre projet, et le contrôle de franchissement de frontière
    perdrait son témoin le plus simple.
    """
    pk = make_unit_project_key("rs")
    foreign_pk = make_unit_project_key("rs-foreign")
    mine = await _seed_learning(session_factory, pk, ["alpha"])
    theirs = await _seed_learning(session_factory, foreign_pk, ["alpha"])

    taken = await snapshot_tags(session_factory, pk)

    assert str(mine) in taken
    assert str(theirs) not in taken


@pytest.mark.asyncio
async def test_an_entity_without_tags_is_present_with_an_empty_list(
    session_factory: async_sessionmaker[AsyncSession],
) -> None:
    """`[]` et « absent » doivent rester deux faits distincts.

    Les confondre ferait passer pour « créée pendant la phase » une entité qui
    n'avait simplement aucun tag — et c'est le cas le plus fréquent du corpus
    que REORG est chargé de normaliser.
    """
    pk = make_unit_project_key("rs")
    lid = await _seed_learning(session_factory, pk, [])

    taken = await snapshot_tags(session_factory, pk)

    assert str(lid) in taken
    assert taken[str(lid)] == []


def test_the_cli_writes_json_the_validator_can_read(
    capsys: pytest.CaptureFixture[str],
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    """Le contrat de transport : un objet JSON id → liste de tags, sur stdout.

    Test SYNCHRONE à dessein : `main` ouvre sa propre boucle avec `asyncio.run`,
    qui refuse de s'imbriquer dans celle de pytest-asyncio. Un test `async` ici
    échouerait sur le harnais et ne dirait rien du contrat.
    """
    from scripts.dream import reorg_snapshot

    url = require_test_db_url()
    pk = make_unit_project_key("rs")

    async def _seed() -> uuid.UUID:
        engine = create_async_engine(url, poolclass=NullPool)
        try:
            factory = async_sessionmaker(engine, class_=AsyncSession, expire_on_commit=False)
            return await _seed_learning(factory, pk, ["alpha"])
        finally:
            await engine.dispose()

    lid = asyncio.run(_seed())
    monkeypatch.setattr(
        reorg_snapshot,
        "Settings",
        lambda: type("S", (), {"postgres_url": url})(),
    )

    assert reorg_snapshot.main(["--project-key", pk]) == 0

    payload = json.loads(capsys.readouterr().out)
    assert payload[str(lid)] == ["alpha"]


def test_the_cli_requires_a_perimeter() -> None:
    """Même garde que les trois validateurs : pas d'instantané sans périmètre.

    Un instantané muet sur son projet produirait la panne la plus coûteuse de
    ce lot : un « avant » pris sur le mauvais corpus, donc des comparaisons
    fausses que rien ne signalerait.
    """
    from scripts.dream import reorg_snapshot

    with pytest.raises(SystemExit) as excinfo:
        reorg_snapshot.main([])

    assert excinfo.value.code == 2
