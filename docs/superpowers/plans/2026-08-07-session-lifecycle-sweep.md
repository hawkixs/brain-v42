# Tarissement des sessions fantômes — plan d'implémentation

> **For agentic workers:** REQUIRED SUB-SKILL: Use superpowers:subagent-driven-development (recommended) or superpowers:executing-plans to implement this plan task-by-task. Steps use checkbox (`- [ ]`) syntax for tracking.

**Spec** : `docs/superpowers/specs/2026-08-07-session-lifecycle-sweep-design.md` (commit `2eb98583`, validée par l'opérateur)
**Ticket brain** : `2bd14b24-ccfe-4372-adf2-245b00304402`

**Goal:** Donner au serveur — et à lui seul — le droit d'abandonner une session ouverte sans signe de vie depuis 7 jours, via une phase Dream `sweep` qui démarre en DRY.

**Architecture:** Le SQL de `brain_sessions` reste entièrement dans `PgBrainSessionRepo`, qui gagne une méthode `abandon_stale` sans garde d'identité (le serveur n'est pas un client). Un CLI `brain_v42.maintenance.session_sweep` porte la politique (seuil, DRY/WET, rapport, row `dream_runs`), sur le modèle de `reap_stale_mcp` pour la forme et de `roadmap_curate` pour l'intégration Dream. `scripts/dream.sh` l'appelle comme il appelle `extract` et `roadmap`. Aucune migration : `dream_runs.phase` est un `varchar(10)` sans contrainte d'énumération, et `sweep` n'écrit que des colonnes existantes.

**Tech Stack:** Python 3.12, SQLAlchemy 2.0 async, asyncpg, Pydantic 2, pytest / pytest-asyncio, bash.

## Global Constraints

Valeurs recopiées **verbatim** de la spec. Toute tâche les respecte implicitement.

- Prédicat : `status = 'open' AND last_heartbeat_at < now() - interval '7 days'`.
- État terminal : `abandoned`, **jamais** `ended` (D2).
- `abandonment_reason` : la constante exacte `auto_stale_7d`, distincte à jamais d'un abandon manuel.
- Portée : **tous les projets**, sans filtre.
- Trace : une ligne `dream_runs`, `phase='sweep'`, `model` NULL, `phase_dry_run` reflétant le mode.
- Killswitches : `BRAIN_DREAM_SWEEP_ENABLED`, `BRAIN_DREAM_SWEEP_DRY_RUN`.
- Défauts livrés : **fermé** (`ENABLED=false`) et **DRY** (`DRY_RUN=true`).
- En DRY : journaliser exactement ce qui **aurait** été abandonné, n'écrire **rien** dans `brain_sessions`.
- L'abandon automatique ne produit ni `summary` ni `next_focus`, et ne touche **jamais** le focus du projet.
- Pas d'`unabandon` : hors périmètre tant que le DRY n'a produit aucun faux positif.
- Le seuil de 7 jours vit dans **une seule** constante Python (`AUTO_STALE_AFTER`). Aucun autre fichier — shell inclus — ne le recopie (learning `8dc7e042` : une constante dupliquée est une bombe à retardement).

### État mesuré le 2026-08-07 (re-mesuré, pas recopié de la spec)

```
21 open · 17 stale à 7j · 4 vivantes
fantôme le plus récent : 10,6 j   ·   vivante la plus ancienne : 0,4 j   →   fossé de 10,2 j
schéma production : 041
```

Le fossé confirme D3. **Il devra être re-mesuré avant le flip WET**, pas relu ici.

---

## Structure des fichiers

| Fichier | Responsabilité | Tâche |
|---|---|---|
| `src/brain_v42/models/brain_session.py` | constantes `AUTO_STALE_AFTER` / `AUTO_STALE_ABANDONMENT_REASON`, modèles de résultat du balayage | 1 |
| `src/brain_v42/repositories/pg_brain_session.py` | `abandon_stale` — le seul écrivain SQL de `brain_sessions` | 1 |
| `src/brain_v42/maintenance/session_sweep.py` | CLI : politique, rapport, row `dream_runs` | 2 |
| `scripts/dream.sh` | phase `sweep` + killswitches | 3 |
| `src/brain_v42/dream_killswitches.py` | lecture du drop-in systemd | 4 |
| `src/brain_v42/services/dream_run_service.py` | `KillswitchState` | 4 |
| `src/brain_v42/mcp/tools/session_tools.py` | ligne SWEEP du briefing | 4 |
| `src/brain_v42/metrics/collector_dream.py` | phase attendue quand le killswitch est ouvert | 4 |
| `CLAUDE.md`, `README.md`, `docs/MCP_TOOLS.md` | amendement doctrinal | 5 |

---

### Task 1 : le balayage côté persistance

**Files:**
- Modify: `src/brain_v42/models/brain_session.py` (constantes après `SESSION_STALE_AFTER:14`, modèles après `BrainSessionAbandonResult:278`)
- Modify: `src/brain_v42/repositories/pg_brain_session.py` (nouvelle méthode après `abandon`, qui finit ligne 487)
- Test: `tests/unit/repositories/test_pg_brain_session_sweep.py` (créer)
- Test: `tests/integration/db/test_brain_sessions_sweep.py` (créer)

**Interfaces:**
- Consomme : `PgBrainSessionRepo(BasePgRepository)`, son `self.transaction()`, la table `brain_sessions`.
- Produit : `AUTO_STALE_AFTER: timedelta`, `AUTO_STALE_ABANDONMENT_REASON: str`, `BrainSessionSweepCandidate`, `BrainSessionSweepResult`, et
  `PgBrainSessionRepo.abandon_stale(*, older_than: timedelta = AUTO_STALE_AFTER, reason: str = AUTO_STALE_ABANDONMENT_REASON, dry_run: bool = True, now: datetime | None = None) -> BrainSessionSweepResult`.

**Note de conception à ne pas perdre :** `abandon_stale` ne prend **pas** de `expected_client_key`. Ce n'est pas un oubli : la garde d'identité protège un client d'en viser un autre, et ici aucun client ne demande rien. Passer la `client_key` de la ligne à elle-même simulerait une vérification qui ne vérifie rien. L'amendement doctrinal de la Task 5 est ce qui autorise ce chemin ; les deux se relisent ensemble.

**Deuxième note :** en WET, **un seul** statement. Pas de `SELECT` puis `UPDATE` : sous READ COMMITTED, PostgreSQL réévalue le `WHERE` sous le verrou de ligne, donc un `heartbeat` qui commit pendant le balayage retire sa ligne de l'update au lieu de perdre la course. C'est la réponse directe au faux-mort du 2026-08-06 (session `9b6f7e18` abandonnée vivante).

- [ ] **Step 1 : écrire les tests unitaires qui échouent**

Créer `tests/unit/repositories/test_pg_brain_session_sweep.py`. Le harnais de mocks du module voisin (`tests/unit/repositories/test_pg_brain_session.py`) est réutilisé par import : il ne fait pas de I/O.

```python
"""Contrat unitaire du balayage serveur des sessions sans signe de vie.

Le harnais compile les statements SQLAlchemy sans PostgreSQL : il prouve la
FORME du prédicat et le fait que le DRY n'émet aucun UPDATE. La frontière
réelle du prédicat (N-1 / N+1 jour) ne se prouve que contre une vraie base :
elle vit dans tests/integration/db/test_brain_sessions_sweep.py.
"""

from __future__ import annotations

from datetime import UTC, datetime, timedelta
from typing import Any
from uuid import uuid4

import pytest

from tests.unit.repositories.test_pg_brain_session import (
    _is_update,
    _make_session,
    _params,
    _result,
    _sql,
)

NOW = datetime(2026, 8, 7, 6, 0, tzinfo=UTC)


def _stale_row(*, project_key: str = "auto-discord", days: float = 24.1) -> dict[str, Any]:
    return {
        "id": uuid4(),
        "project_key": project_key,
        "client_key": "codex-factory-28aeb338",
        "last_heartbeat_at": NOW - timedelta(days=days),
    }


def _router(rows: list[dict[str, Any]]):
    def route(statement: Any):
        return _result(rows=rows)

    return route


@pytest.mark.asyncio
async def test_dry_run_selects_and_never_updates() -> None:
    from brain_v42.repositories.pg_brain_session import PgBrainSessionRepo

    row = _stale_row()
    _, statements, _, factory = _make_session(_router([row]))

    result = await PgBrainSessionRepo(factory).abandon_stale(dry_run=True, now=NOW)

    assert [candidate.project_key for candidate in result.candidates] == ["auto-discord"]
    assert result.dry_run is True
    assert result.abandoned_count == 0
    assert not [stmt for stmt in statements if _is_update(stmt, "brain_sessions")]
    assert len(statements) == 1
    assert _sql(statements[0]).startswith("select")


@pytest.mark.asyncio
async def test_wet_run_updates_in_a_single_statement() -> None:
    from brain_v42.repositories.pg_brain_session import PgBrainSessionRepo

    row = _stale_row()
    _, statements, _, factory = _make_session(_router([row]))

    result = await PgBrainSessionRepo(factory).abandon_stale(dry_run=False, now=NOW)

    assert result.dry_run is False
    assert result.abandoned_count == 1
    updates = [stmt for stmt in statements if _is_update(stmt, "brain_sessions")]
    assert len(updates) == 1
    assert len(statements) == 1, "un seul statement : pas de fenêtre SELECT-puis-UPDATE"
    sql = _sql(updates[0])
    assert "returning" in sql
    assert "status" in sql and "abandonment_reason" in sql and "ended_at" in sql
    assert "summary" not in sql
    assert "next_focus" not in sql
    assert "project_contexts" not in sql


@pytest.mark.asyncio
async def test_cutoff_is_now_minus_threshold_and_strict() -> None:
    from brain_v42.models.brain_session import AUTO_STALE_AFTER
    from brain_v42.repositories.pg_brain_session import PgBrainSessionRepo

    _, statements, _, factory = _make_session(_router([]))

    result = await PgBrainSessionRepo(factory).abandon_stale(dry_run=True, now=NOW)

    assert AUTO_STALE_AFTER == timedelta(days=7)
    assert result.cutoff == NOW - timedelta(days=7)
    sql = _sql(statements[0])
    assert "status =" in sql
    assert "last_heartbeat_at <" in sql
    assert "last_heartbeat_at <=" not in sql


@pytest.mark.asyncio
async def test_default_reason_is_the_auto_constant() -> None:
    from brain_v42.models.brain_session import AUTO_STALE_ABANDONMENT_REASON
    from brain_v42.repositories.pg_brain_session import PgBrainSessionRepo

    _, statements, _, factory = _make_session(_router([_stale_row()]))

    await PgBrainSessionRepo(factory).abandon_stale(dry_run=False, now=NOW)

    assert AUTO_STALE_ABANDONMENT_REASON == "auto_stale_7d"
    assert "auto_stale_7d" in _params(statements[0]).values()


@pytest.mark.asyncio
@pytest.mark.parametrize("bad", ["", "   "])
async def test_blank_reason_is_refused(bad: str) -> None:
    from brain_v42.models.brain_session import BrainSessionInputError
    from brain_v42.repositories.pg_brain_session import PgBrainSessionRepo

    _, _, _, factory = _make_session(_router([]))

    with pytest.raises(BrainSessionInputError):
        await PgBrainSessionRepo(factory).abandon_stale(reason=bad, dry_run=False, now=NOW)


@pytest.mark.asyncio
async def test_non_positive_threshold_is_refused() -> None:
    from brain_v42.models.brain_session import BrainSessionInputError
    from brain_v42.repositories.pg_brain_session import PgBrainSessionRepo

    _, _, _, factory = _make_session(_router([]))

    with pytest.raises(BrainSessionInputError):
        await PgBrainSessionRepo(factory).abandon_stale(
            older_than=timedelta(0), dry_run=False, now=NOW
        )
```

- [ ] **Step 2 : vérifier l'échec pour la bonne raison**

```bash
unset VIRTUAL_ENV
uv run pytest tests/unit/repositories/test_pg_brain_session_sweep.py -v
```
Attendu : `ImportError` sur `AUTO_STALE_AFTER` / `AttributeError: 'PgBrainSessionRepo' object has no attribute 'abandon_stale'`. Si l'échec vient de `_is_update` ou `_make_session`, l'import du harnais est faux — corriger l'import, pas le test.

- [ ] **Step 3 : ajouter les constantes et les modèles**

Dans `src/brain_v42/models/brain_session.py`, juste après `SESSION_STALE_AFTER` (ligne 14) :

```python
SESSION_STALE_AFTER = timedelta(hours=24)
# Deux seuils distincts, volontairement côte à côte pour qu'on ne les confonde
# jamais : SESSION_STALE_AFTER (24 h) est un flag DÉRIVÉ affiché au client, il
# ne change aucun statut ; AUTO_STALE_AFTER (7 j) est le seuil auquel le
# SERVEUR abandonne. Le fossé mesuré le 2026-08-07 entre le fantôme le plus
# récent (10,6 j) et la vivante la plus ancienne (0,4 j) calibre le second.
AUTO_STALE_AFTER = timedelta(days=7)
AUTO_STALE_ABANDONMENT_REASON = "auto_stale_7d"
```

À la fin du fichier, après `BrainSessionListResult` :

```python
class BrainSessionSweepCandidate(BaseModel):
    """Une session ouverte retenue par le balayage, en DRY comme en WET."""

    id: UUID
    project_key: str
    client_key: str
    last_heartbeat_at: datetime


class BrainSessionSweepResult(BaseModel):
    """Résultat d'un balayage serveur, tous projets confondus."""

    candidates: list[BrainSessionSweepCandidate]
    dry_run: bool
    cutoff: datetime
    # Toujours 0 en DRY. Redondant avec len(candidates) — délibérément : un
    # journal doit rendre « 17 auraient été abandonnées » illisible comme
    # « 17 ont été abandonnées ».
    abandoned_count: int = Field(..., ge=0)
```

- [ ] **Step 4 : implémenter `abandon_stale`**

Dans `src/brain_v42/repositories/pg_brain_session.py`, ajouter aux imports depuis `brain_v42.models.brain_session` :

```python
    AUTO_STALE_ABANDONMENT_REASON,
    AUTO_STALE_AFTER,
    BrainSessionSweepCandidate,
    BrainSessionSweepResult,
```

Ajouter aux imports standard : `from datetime import UTC, datetime, timedelta` (la ligne 7 existe déjà, ajouter `timedelta`).

Puis, juste après la méthode `abandon` (qui se termine ligne 487) :

```python
    async def abandon_stale(
        self,
        *,
        older_than: timedelta = AUTO_STALE_AFTER,
        reason: str = AUTO_STALE_ABANDONMENT_REASON,
        dry_run: bool = True,
        now: datetime | None = None,
    ) -> BrainSessionSweepResult:
        """Abandonner toute session ouverte sans heartbeat depuis ``older_than``.

        Chemin SERVEUR uniquement : pas de garde ``expected_client_key``, parce
        qu'aucun client ne demande — c'est le serveur. L'amendement doctrinal du
        CLAUDE.md borne ce droit à ce seul chemin ; il n'ouvre rien pour l'agent
        ni pour le client, dont les sept commandes restent explicites.

        Ne touche ni ``project_contexts`` ni ``brain_session_artifacts`` : le
        focus et le ledger de capture d'une session abandonnée survivent, comme
        pour un abandon manuel.
        """
        normalized_reason = reason.strip()
        if not normalized_reason:
            raise BrainSessionInputError("abandonment reason must not be blank")
        if older_than <= timedelta(0):
            raise BrainSessionInputError("older_than must be a positive interval")

        reference = now or datetime.now(UTC)
        cutoff = reference - older_than
        stale = sa.and_(
            brain_sessions.c.status == "open",
            brain_sessions.c.last_heartbeat_at < cutoff,
        )
        selection = (
            brain_sessions.c.id,
            brain_sessions.c.project_key,
            brain_sessions.c.client_key,
            brain_sessions.c.last_heartbeat_at,
        )

        if dry_run:
            statement: Any = sa.select(*selection).where(stale)
        else:
            # UN SEUL statement. Pas de SELECT puis UPDATE : sous READ
            # COMMITTED, PostgreSQL réévalue `stale` sous le verrou de ligne,
            # donc un heartbeat qui commit pendant le balayage retire sa ligne
            # de l'update au lieu de perdre la course. C'est la réponse au
            # faux-mort du 2026-08-06 (session vivante abandonnée à tort).
            statement = (
                brain_sessions.update()
                .where(stale)
                .values(
                    status="abandoned",
                    abandonment_reason=normalized_reason,
                    ended_at=reference,
                    updated_at=reference,
                )
                .returning(*selection)
            )

        async with self.transaction() as session:
            rows = (await session.execute(statement)).mappings().all()

        candidates = sorted(
            (BrainSessionSweepCandidate(**dict(row)) for row in rows),
            key=lambda candidate: candidate.last_heartbeat_at,
        )
        return BrainSessionSweepResult(
            candidates=candidates,
            dry_run=dry_run,
            cutoff=cutoff,
            abandoned_count=0 if dry_run else len(candidates),
        )
```

- [ ] **Step 5 : vérifier que les tests unitaires passent**

```bash
unset VIRTUAL_ENV
uv run pytest tests/unit/repositories/test_pg_brain_session_sweep.py -v
```
Attendu : 7 passed.

- [ ] **Step 6 : écrire les tests d'intégration qui échouent**

Créer `tests/integration/db/test_brain_sessions_sweep.py`. **Le seuil de 365 jours n'est pas décoratif** : le balayage est global par conception, et la base d'intégration est partagée avec les autres fixtures. Antidater les lignes du test et viser 365 jours garantit qu'aucune session créée par un test voisin ne peut entrer dans le périmètre.

```python
"""Le balayage serveur contre une vraie base : frontière et invariants.

Seuil de 365 jours partout : le balayage est GLOBAL par conception, et la base
d'intégration est partagée. Antidater les lignes de la fixture et viser un an
rend structurellement impossible d'emporter la session d'un test voisin, qui
est forcément créée « maintenant ».
"""

from __future__ import annotations

from collections.abc import AsyncIterator
from datetime import UTC, datetime, timedelta
from uuid import uuid4

import pytest
import pytest_asyncio
import sqlalchemy as sa
from sqlalchemy.ext.asyncio import AsyncSession, async_sessionmaker

from brain_v42.db.tables import (
    brain_session_artifacts,
    brain_sessions,
    decisions,
    project_contexts,
)
from brain_v42.repositories.pg_brain_session import PgBrainSessionRepo

pytestmark = pytest.mark.integration

THRESHOLD = timedelta(days=365)


@pytest_asyncio.fixture
async def sweep_project(
    session_factory: async_sessionmaker[AsyncSession],
) -> AsyncIterator[str]:
    project_key = f"integ-sweep-{uuid4().hex[:10]}"
    async with session_factory.begin() as session:
        await session.execute(
            project_contexts.insert().values(
                project_key=project_key,
                name="Session sweep integration",
                description="Isolated sweep fixture",
                current_focus="focus avant balayage",
            )
        )
    try:
        yield project_key
    finally:
        async with session_factory.begin() as session:
            project_sessions = sa.select(brain_sessions.c.id).where(
                brain_sessions.c.project_key == project_key
            )
            await session.execute(
                brain_session_artifacts.delete().where(
                    brain_session_artifacts.c.session_id.in_(project_sessions)
                )
            )
            await session.execute(
                brain_sessions.delete().where(brain_sessions.c.project_key == project_key)
            )
            await session.execute(decisions.delete().where(decisions.c.project_key == project_key))
            await session.execute(
                project_contexts.delete().where(project_contexts.c.project_key == project_key)
            )


async def _insert_open_session(
    session_factory: async_sessionmaker[AsyncSession],
    project_key: str,
    client_key: str,
    heartbeat: datetime,
):
    async with session_factory.begin() as session:
        row = (
            await session.execute(
                brain_sessions.insert()
                .values(
                    project_key=project_key,
                    client_key=client_key,
                    started_focus="focus avant balayage",
                    started_focus_revision=1,
                    started_at=heartbeat,
                    last_heartbeat_at=heartbeat,
                )
                .returning(brain_sessions.c.id)
            )
        ).scalar_one()
    return row


async def _read(session_factory: async_sessionmaker[AsyncSession], session_id):
    async with session_factory() as session:
        return (
            (
                await session.execute(
                    sa.select(brain_sessions).where(brain_sessions.c.id == session_id)
                )
            )
            .mappings()
            .one()
        )


async def test_predicate_boundary_spares_n_minus_one_and_takes_n_plus_one(
    session_factory: async_sessionmaker[AsyncSession],
    sweep_project: str,
) -> None:
    now = datetime.now(UTC)
    inside = await _insert_open_session(
        session_factory, sweep_project, "inside", now - THRESHOLD + timedelta(days=1)
    )
    outside = await _insert_open_session(
        session_factory, sweep_project, "outside", now - THRESHOLD - timedelta(days=1)
    )

    result = await PgBrainSessionRepo(session_factory).abandon_stale(
        older_than=THRESHOLD, dry_run=False, now=now
    )

    swept = {candidate.id for candidate in result.candidates}
    assert outside in swept
    assert inside not in swept
    assert (await _read(session_factory, inside))["status"] == "open"
    abandoned = await _read(session_factory, outside)
    assert abandoned["status"] == "abandoned"
    assert abandoned["abandonment_reason"] == "auto_stale_7d"
    assert abandoned["ended_at"] is not None
    assert abandoned["summary"] is None
    assert abandoned["next_focus"] is None
    assert abandoned["focus_outcome"] is None


async def test_dry_run_writes_nothing(
    session_factory: async_sessionmaker[AsyncSession],
    sweep_project: str,
) -> None:
    now = datetime.now(UTC)
    ghost = await _insert_open_session(
        session_factory, sweep_project, "ghost", now - THRESHOLD - timedelta(days=1)
    )
    before = await _read(session_factory, ghost)

    result = await PgBrainSessionRepo(session_factory).abandon_stale(
        older_than=THRESHOLD, dry_run=True, now=now
    )

    assert [candidate.id for candidate in result.candidates] == [ghost]
    assert result.abandoned_count == 0
    assert dict(await _read(session_factory, ghost)) == dict(before)


async def test_sweep_preserves_focus_revision_and_attributions(
    session_factory: async_sessionmaker[AsyncSession],
    sweep_project: str,
) -> None:
    now = datetime.now(UTC)
    ghost = await _insert_open_session(
        session_factory, sweep_project, "ghost-with-capture", now - THRESHOLD - timedelta(days=2)
    )
    knowledge_id = uuid4()
    async with session_factory.begin() as session:
        await session.execute(
            decisions.insert().values(
                id=knowledge_id,
                project_key=sweep_project,
                title="décision capturée avant le balayage",
                content="corps",
            )
        )
        await session.execute(
            brain_session_artifacts.insert().values(
                session_id=ghost,
                knowledge_id=knowledge_id,
                knowledge_type="decision",
            )
        )
    async with session_factory() as session:
        focus_before = (
            (
                await session.execute(
                    sa.select(
                        project_contexts.c.current_focus,
                        project_contexts.c.focus_revision,
                        project_contexts.c.focus_updated_at,
                    ).where(project_contexts.c.project_key == sweep_project)
                )
            )
            .mappings()
            .one()
        )

    await PgBrainSessionRepo(session_factory).abandon_stale(
        older_than=THRESHOLD, dry_run=False, now=now
    )

    async with session_factory() as session:
        focus_after = (
            (
                await session.execute(
                    sa.select(
                        project_contexts.c.current_focus,
                        project_contexts.c.focus_revision,
                        project_contexts.c.focus_updated_at,
                    ).where(project_contexts.c.project_key == sweep_project)
                )
            )
            .mappings()
            .one()
        )
        attributions = (
            (
                await session.execute(
                    sa.select(brain_session_artifacts.c.knowledge_id).where(
                        brain_session_artifacts.c.session_id == ghost
                    )
                )
            )
            .scalars()
            .all()
        )

    assert dict(focus_after) == dict(focus_before)
    assert list(attributions) == [knowledge_id]
    swept = await _read(session_factory, ghost)
    assert swept["status"] == "abandoned"
    # Le snapshot terminal reste vide : c'est la contrainte CHECK
    # brain_sessions_terminal_state_valid pour 'abandoned'. Le ledger, lui,
    # vit dans brain_session_artifacts et survit.
    assert list(swept["captured_knowledge_ids"]) == []


async def test_manual_abandonment_reason_is_never_overwritten(
    session_factory: async_sessionmaker[AsyncSession],
    sweep_project: str,
) -> None:
    now = datetime.now(UTC)
    manual = await _insert_open_session(
        session_factory, sweep_project, "manual", now - THRESHOLD - timedelta(days=3)
    )
    async with session_factory.begin() as session:
        await session.execute(
            brain_sessions.update()
            .where(brain_sessions.c.id == manual)
            .values(
                status="abandoned",
                abandonment_reason="abandon manuel de l'opérateur",
                ended_at=now,
            )
        )

    result = await PgBrainSessionRepo(session_factory).abandon_stale(
        older_than=THRESHOLD, dry_run=False, now=now
    )

    assert manual not in {candidate.id for candidate in result.candidates}
    row = await _read(session_factory, manual)
    assert row["abandonment_reason"] == "abandon manuel de l'opérateur"
```

- [ ] **Step 7 : vérifier l'échec des tests d'intégration**

```bash
unset VIRTUAL_ENV
GRAPH_LEDGER_WRITE_ENABLED=false BRAIN_V42_TEST_DB_URL=postgresql+asyncpg://brain:brain@localhost:5433/brain_test uv run pytest tests/integration/db/test_brain_sessions_sweep.py -v
```
Attendu : 4 tests qui échouent (`AttributeError` si l'étape 4 n'est pas encore faite, sinon des assertions). Si les tests **SKIP** malgré la variable, la base d'intégration n'est pas joignable — la fixture DB vit dans `tests/integration/conftest.py`, un test DB sous `tests/unit/` skippe en silence.

- [ ] **Step 8 : faire passer l'intégration**

Aucun code neuf attendu si l'étape 4 est correcte. Deux échecs plausibles à traiter :
- `decisions.insert()` refusé faute de colonne obligatoire → lire `src/brain_v42/db/tables.py` et compléter les valeurs, **sans** toucher au code de production.
- `CheckViolationError` sur `brain_sessions_terminal_state_valid` → la clause `values()` de `abandon_stale` écrit une colonne interdite pour `abandoned` ; la retirer.

```bash
unset VIRTUAL_ENV
GRAPH_LEDGER_WRITE_ENABLED=false BRAIN_V42_TEST_DB_URL=postgresql+asyncpg://brain:brain@localhost:5433/brain_test uv run pytest tests/integration/db/test_brain_sessions_sweep.py -v
```
Attendu : `4 passed` — un compte non nul, pas « 4 skipped ».

- [ ] **Step 9 : gates puis commit**

```bash
unset VIRTUAL_ENV
uv run ruff check src/ tests/ && uv run ruff format --check src/ tests/ && uv run mypy src/
uv run pytest tests/unit/repositories tests/unit/models -q
git add src/brain_v42/models/brain_session.py src/brain_v42/repositories/pg_brain_session.py \
        tests/unit/repositories/test_pg_brain_session_sweep.py \
        tests/integration/db/test_brain_sessions_sweep.py
git commit -m "feat(sessions): abandonner côté serveur les sessions sans signe de vie depuis 7 jours"
```

---

### Task 2 : le CLI de la phase

**Files:**
- Create: `src/brain_v42/maintenance/session_sweep.py`
- Test: `tests/unit/maintenance/test_session_sweep.py` (créer, avec `tests/unit/maintenance/__init__.py` si le paquet n'existe pas)

**Interfaces:**
- Consomme : `PgBrainSessionRepo.abandon_stale`, `AUTO_STALE_AFTER`, `BrainSessionSweepResult` (Task 1).
- Produit : `build_parser() -> argparse.ArgumentParser`, `render_report(result: BrainSessionSweepResult) -> str`, `record_dream_run(session_factory, status, dry, duration_s, error) -> None`, `main() -> int`. Point d'entrée : `python -m brain_v42.maintenance.session_sweep`.

- [ ] **Step 1 : écrire les tests qui échouent**

Créer `tests/unit/maintenance/__init__.py` (vide) puis `tests/unit/maintenance/test_session_sweep.py` :

```python
"""Contrat du CLI de balayage : DRY par défaut, seuil non dupliqué, rapport lisible."""

from __future__ import annotations

from datetime import UTC, datetime, timedelta
from uuid import uuid4

import pytest

from brain_v42.models.brain_session import (
    AUTO_STALE_AFTER,
    BrainSessionSweepCandidate,
    BrainSessionSweepResult,
)

NOW = datetime(2026, 8, 7, 6, 0, tzinfo=UTC)


def _result(*, dry_run: bool, count: int = 2) -> BrainSessionSweepResult:
    candidates = [
        BrainSessionSweepCandidate(
            id=uuid4(),
            project_key=f"projet-{index}",
            client_key=f"codex-factory-{index}",
            last_heartbeat_at=NOW - timedelta(days=10 + index),
        )
        for index in range(count)
    ]
    return BrainSessionSweepResult(
        candidates=candidates,
        dry_run=dry_run,
        cutoff=NOW - AUTO_STALE_AFTER,
        abandoned_count=0 if dry_run else count,
    )


def test_dry_is_the_default_mode() -> None:
    from brain_v42.maintenance.session_sweep import build_parser

    args = build_parser().parse_args([])

    assert args.wet is False


def test_threshold_default_comes_from_the_single_constant() -> None:
    from brain_v42.maintenance.session_sweep import build_parser

    args = build_parser().parse_args([])

    assert args.older_than_days == AUTO_STALE_AFTER.days == 7


def test_non_positive_threshold_is_refused() -> None:
    from brain_v42.maintenance.session_sweep import build_parser

    with pytest.raises(SystemExit):
        build_parser().parse_args(["--older-than-days", "0"])


def test_dry_report_says_would_and_never_says_abandoned() -> None:
    from brain_v42.maintenance.session_sweep import render_report

    report = render_report(_result(dry_run=True))

    assert "DRY" in report
    assert "auraient été abandonnées" in report
    assert "ont été abandonnées" not in report
    assert "projet-0" in report and "projet-1" in report
    assert "2026-07-31" in report  # cutoff rendu, pas seulement le compte


def test_wet_report_states_what_was_written() -> None:
    from brain_v42.maintenance.session_sweep import render_report

    report = render_report(_result(dry_run=False))

    assert "WET" in report
    assert "2 sessions ont été abandonnées" in report
    assert "auraient" not in report


def test_empty_sweep_is_reported_as_a_normal_night() -> None:
    from brain_v42.maintenance.session_sweep import render_report

    report = render_report(_result(dry_run=True, count=0))

    assert "aucune session à abandonner" in report
    assert len(report.splitlines()) == 1, "aucune ligne de candidat"


@pytest.mark.asyncio
async def test_record_dream_run_never_raises_when_the_database_is_down() -> None:
    from brain_v42.maintenance.session_sweep import record_dream_run

    def broken_factory():
        raise RuntimeError("base injoignable")

    await record_dream_run(
        broken_factory, "done", dry=True, duration_s=1.0, error=None
    )  # ne doit pas lever
```

- [ ] **Step 2 : vérifier l'échec**

```bash
unset VIRTUAL_ENV
uv run pytest tests/unit/maintenance/test_session_sweep.py -v
```
Attendu : `ModuleNotFoundError: No module named 'brain_v42.maintenance.session_sweep'`.

- [ ] **Step 3 : écrire le CLI**

Créer `src/brain_v42/maintenance/session_sweep.py` :

```python
"""Phase Dream `sweep` — tarir les sessions ouvertes sans signe de vie.

Spec : docs/superpowers/specs/2026-08-07-session-lifecycle-sweep-design.md

Déterministe et sans modèle : aucun appel LLM, aucun réseau. La row
``dream_runs`` porte donc ``model = NULL`` — forme déjà admise, observée sur
``extract`` et sur le run ``roadmap`` du 2026-08-05.

Livré DRY : ``--wet`` est le seul chemin qui écrit.

Usage:
    python -m brain_v42.maintenance.session_sweep           # dry (défaut)
    python -m brain_v42.maintenance.session_sweep --wet     # applique
"""

from __future__ import annotations

import argparse
import asyncio
import sys
import time
from datetime import date, timedelta
from typing import Any

import sqlalchemy as sa

from brain_v42.models.brain_session import AUTO_STALE_AFTER, BrainSessionSweepResult

_MAX_ERROR_CHARS = 2000


def _positive_int(value: str) -> int:
    number = int(value)
    if number < 1:
        raise argparse.ArgumentTypeError(f"doit être >= 1 (reçu : {number})")
    return number


def build_parser() -> argparse.ArgumentParser:
    parser = argparse.ArgumentParser(
        prog="session_sweep",
        description="Abandonner les sessions ouvertes sans heartbeat depuis N jours.",
    )
    parser.add_argument(
        "--wet",
        action="store_true",
        help="applique les abandons (défaut : dry, aucune écriture)",
    )
    parser.add_argument(
        "--older-than-days",
        type=_positive_int,
        # Défaut LU de la constante, jamais recopié : deux exemplaires d'un
        # même seuil, c'est le défaut de classe du learning 8dc7e042.
        default=AUTO_STALE_AFTER.days,
        help=f"seuil en jours (défaut : {AUTO_STALE_AFTER.days}, depuis AUTO_STALE_AFTER)",
    )
    return parser


def render_report(result: BrainSessionSweepResult) -> str:
    """Rapport texte du balayage, pour le log daté de la nuit."""
    mode = "DRY" if result.dry_run else "WET"
    cutoff = result.cutoff.isoformat(timespec="seconds")
    count = len(result.candidates)
    if count == 0:
        return f"sweep [{mode}] cutoff={cutoff} — aucune session à abandonner"

    verb = "auraient été abandonnées" if result.dry_run else "ont été abandonnées"
    lines = [f"sweep [{mode}] cutoff={cutoff} — {count} sessions {verb}"]
    lines.extend(
        f"  {candidate.project_key:<16} {candidate.client_key:<40} "
        f"{candidate.last_heartbeat_at.isoformat(timespec='seconds')}"
        for candidate in result.candidates
    )
    return "\n".join(lines)


async def record_dream_run(
    session_factory: Any,
    status: str,
    dry: bool,
    duration_s: float,
    error: str | None,
) -> None:
    """INSERT dream_runs pour phase='sweep'. Best-effort — ne lève jamais.

    `model` reste NULL : la phase n'appelle aucun modèle.
    """
    try:
        async with session_factory() as session:
            async with session.begin():
                await session.execute(
                    sa.text(
                        "INSERT INTO dream_runs "
                        "(run_date, phase, status, duration_s, error_message, "
                        "phase_dry_run, model) "
                        "VALUES (:run_date, 'sweep', :status, :duration_s, "
                        ":error_message, :phase_dry_run, NULL)"
                    ),
                    {
                        "run_date": date.today(),
                        "status": status,
                        "duration_s": duration_s,
                        "error_message": error,
                        "phase_dry_run": dry,
                    },
                )
    except Exception as exc:  # noqa: BLE001 — la trace ne doit jamais tuer la phase
        print(f"! warning: could not record dream_run: {exc}", file=sys.stderr)


async def _run(args: argparse.Namespace) -> int:
    from pydantic import ValidationError  # noqa: PLC0415

    from brain_v42.config import Settings  # noqa: PLC0415
    from brain_v42.db.engine import get_session_factory  # noqa: PLC0415
    from brain_v42.repositories.pg_brain_session import PgBrainSessionRepo  # noqa: PLC0415

    try:
        Settings()  # type: ignore[call-arg]
    except ValidationError as exc:
        print(f"Config invalide: {exc}", file=sys.stderr)
        return 2

    session_factory = get_session_factory()
    dry = not args.wet
    started = time.monotonic()
    try:
        result = await PgBrainSessionRepo(session_factory).abandon_stale(
            older_than=timedelta(days=args.older_than_days),
            dry_run=dry,
        )
    except Exception as exc:  # noqa: BLE001 — traduit en row dream_runs + rc=1
        detail = f"{type(exc).__name__}: {exc}"[:_MAX_ERROR_CHARS]
        await record_dream_run(
            session_factory, "fail", dry=dry, duration_s=time.monotonic() - started, error=detail
        )
        print(f"sweep: FAIL — {detail}", file=sys.stderr)
        return 1

    print(render_report(result), flush=True)
    await record_dream_run(
        session_factory, "done", dry=dry, duration_s=time.monotonic() - started, error=None
    )
    return 0


def main() -> int:
    return asyncio.run(_run(build_parser().parse_args()))


if __name__ == "__main__":
    sys.exit(main())
```

- [ ] **Step 4 : vérifier que les tests passent**

```bash
unset VIRTUAL_ENV
uv run pytest tests/unit/maintenance/test_session_sweep.py -v
```
Attendu : 7 passed. Si `test_dry_report_says_would...` échoue sur `"2026-07-31"`, vérifier le calcul du cutoff dans la fixture du test (`NOW - AUTO_STALE_AFTER` = 2026-07-31T06:00) — ne pas assouplir l'assertion : le cutoff DOIT être dans le rapport.

- [ ] **Step 5 : fumer le CLI en DRY contre la production**

Lecture seule par construction (`--wet` absent). C'est la première preuve réelle du mécanisme.

```bash
unset VIRTUAL_ENV
uv run python -m brain_v42.maintenance.session_sweep
```
Attendu : la liste des sessions sans heartbeat depuis 7 jours, ~17 lignes au 2026-08-07, `abandoned_count` implicite à 0. **Vérifier à l'œil qu'aucune session du jour n'apparaît.** Vérifier aussi que la row de trace est écrite :

```bash
docker exec brain_v42_postgres psql -U brain -d brain -c \
  "select run_date, phase, status, phase_dry_run, model, duration_s from dream_runs where phase='sweep' order by id desc limit 3;"
```
Attendu : une ligne `sweep | done | t | (null)`.

- [ ] **Step 6 : gates puis commit**

```bash
unset VIRTUAL_ENV
uv run ruff check src/ tests/ && uv run ruff format --check src/ tests/ && uv run mypy src/
uv run python scripts/check_module_layering.py --package src/brain_v42
git add src/brain_v42/maintenance/session_sweep.py tests/unit/maintenance/
git commit -m "feat(dream): ajouter le CLI de balayage des sessions fantômes"
```

---

### Task 3 : la phase dans `dream.sh`

**Files:**
- Modify: `scripts/dream.sh` (killswitches après la ligne 54 ; bloc de phase après le bloc ROADMAP, qui finit ligne 681)
- Test: `tests/unit/test_dream_sh_sweep.py` (créer)

**Interfaces:**
- Consomme : `python -m brain_v42.maintenance.session_sweep` et son drapeau `--wet` (Task 2).
- Produit : les variables shell `BRAIN_DREAM_SWEEP_ENABLED` et `BRAIN_DREAM_SWEEP_DRY_RUN`, et le log daté `${TIMESTAMP}_sweep.log`.

- [ ] **Step 1 : écrire les tests qui échouent**

Créer `tests/unit/test_dream_sh_sweep.py`, calqué sur `tests/unit/test_dream_sh_roadmap.py` :

```python
"""Épingle le câblage de la phase SWEEP dans dream.sh (grep, sans exécution)."""

from pathlib import Path

_DREAM_SH = Path(__file__).parent.parent.parent / "scripts" / "dream.sh"


def _content() -> str:
    return _DREAM_SH.read_text(encoding="utf-8")


def test_sweep_killswitch_defaults_closed_and_dry():
    content = _content()
    assert 'BRAIN_DREAM_SWEEP_ENABLED="${BRAIN_DREAM_SWEEP_ENABLED:-false}"' in content
    assert 'BRAIN_DREAM_SWEEP_DRY_RUN="${BRAIN_DREAM_SWEEP_DRY_RUN:-true}"' in content


def test_sweep_step_invokes_the_cli_module():
    content = _content()
    assert "brain_v42.maintenance.session_sweep" in content
    assert "SKIP sweep (killswitch" in content


def test_sweep_wet_flag_only_when_dry_run_false():
    content = _content()
    assert 'if [[ "$BRAIN_DREAM_SWEEP_DRY_RUN" != "true" ]]' in content


def test_sweep_step_has_timeout_and_own_log():
    content = _content()
    assert "timeout 5m uv run python -m brain_v42.maintenance.session_sweep" in content
    assert "_sweep.log" in content


def test_sweep_step_does_not_duplicate_the_threshold():
    """Le seuil vit dans AUTO_STALE_AFTER. Une deuxième copie dans le shell
    serait la bombe à retardement du learning 8dc7e042 : deux constantes qui
    se contredisent en silence le jour où l'une bouge."""
    content = _content()
    sweep_block = content.split("--- SWEEP")[1]
    assert "--older-than-days" not in sweep_block
    assert "7" not in sweep_block.split("sweep_args=(")[1].split(")")[0]
```

- [ ] **Step 2 : vérifier l'échec**

```bash
unset VIRTUAL_ENV
uv run pytest tests/unit/test_dream_sh_sweep.py -v
```
Attendu : 5 failed (`assert ... in content`). Le dernier peut lever `IndexError` — c'est le même échec, la section n'existe pas.

- [ ] **Step 3 : déclarer les killswitches**

Dans `scripts/dream.sh`, juste après la ligne 54 (`BRAIN_DREAM_ROADMAP_DRY_RUN=...`) :

```bash
# SWEEP killswitch — tarissement des sessions fantômes (spec 2026-08-07).
# Livré FERMÉ et DRY. Phase déterministe, sans modèle ni réseau : le seuil
# vit dans brain_v42.models.brain_session.AUTO_STALE_AFTER, jamais ici.
BRAIN_DREAM_SWEEP_ENABLED="${BRAIN_DREAM_SWEEP_ENABLED:-false}"
BRAIN_DREAM_SWEEP_DRY_RUN="${BRAIN_DREAM_SWEEP_DRY_RUN:-true}"
```

- [ ] **Step 4 : ajouter le bloc de phase**

Dans `scripts/dream.sh`, entre la fin du bloc ROADMAP (le `fi` de la ligne 681) et la ligne `FAIL_TOTAL=$(( ... ))` :

```bash
# --- SWEEP: tarissement des sessions fantômes ------------------------------
# Pas une phase d'agent : CLI Python direct (pattern extract/roadmap). Insère
# sa propre row dream_runs (phase='sweep', model NULL) pour la visibilité
# briefing. Le seuil n'est PAS passé en argument : une seule constante.
TOTAL_PHASES=$(( TOTAL_PHASES + 1 ))
if [[ "$BRAIN_DREAM_SWEEP_ENABLED" != "true" ]]; then
  log "SKIP sweep (killswitch BRAIN_DREAM_SWEEP_ENABLED=$BRAIN_DREAM_SWEEP_ENABLED)"
  SKIPPED_PHASES+=("sweep")
else
  sweep_args=()
  if [[ "$BRAIN_DREAM_SWEEP_DRY_RUN" != "true" ]]; then
    sweep_args+=(--wet)
  fi
  log "sweep: session_sweep starting (dry_run=$BRAIN_DREAM_SWEEP_DRY_RUN)"
  set +e
  # 5m : une requête indexée, sans appel modèle ni réseau. Un dépassement
  # signale une base en souffrance, pas une phase lente.
  timeout 5m uv run python -m brain_v42.maintenance.session_sweep "${sweep_args[@]}" \
    >> "$LOG_DIR/${TIMESTAMP}_sweep.log" 2>&1
  sweep_rc=$?
  set -e
  if (( sweep_rc == 0 )); then
    log "DONE sweep"
  else
    log "FAIL sweep (rc=$sweep_rc) — see ${TIMESTAMP}_sweep.log"
    FAILED_PHASES+=("sweep")
  fi
fi
```

**Attention `set -u` :** `"${sweep_args[@]}"` sur un tableau vide échoue en bash < 4.4. Vérifier `bash --version` ≥ 4.4 (le cas sur cet hôte) ; sinon écrire `${sweep_args[@]+"${sweep_args[@]}"}`.

- [ ] **Step 5 : vérifier que les tests passent et que le script reste valide**

```bash
unset VIRTUAL_ENV
bash -n scripts/dream.sh
uv run pytest tests/unit/test_dream_sh_sweep.py tests/unit/test_dream_sh_roadmap.py tests/unit/test_dream_sh_extract.py -v
```
Attendu : `bash -n` silencieux, tous les tests passent.

- [ ] **Step 6 : commit**

```bash
git add scripts/dream.sh tests/unit/test_dream_sh_sweep.py
git commit -m "feat(dream): câbler la phase sweep derrière son killswitch, fermé et dry"
```

---

### Task 4 : rendre la phase visible

Sans cette tâche, un opérateur ne peut pas voir depuis le briefing si le balayage est armé, et les métriques ne s'attendent pas à la phase — un `sweep` manquant passerait pour une nuit normale.

**Files:**
- Modify: `src/brain_v42/dream_killswitches.py:12-20` (`_KS_KEYS`)
- Modify: `src/brain_v42/services/dream_run_service.py:38-52` (`KillswitchState`) et `:134-152`
- Modify: `src/brain_v42/mcp/tools/session_tools.py:106-128` (`_section_killswitches`)
- Modify: `src/brain_v42/metrics/collector_dream.py:45` (`expected_dream_phases`)
- Modify: `tests/fixtures/briefing_full.md:3-8` (golden)
- Test: `tests/unit/services/test_dream_run_service.py`, `tests/unit/mcp/test_session_tools.py`, `tests/unit/metrics/test_dream_metrics.py`

**Interfaces:**
- Consomme : les clés d'environnement `BRAIN_DREAM_SWEEP_ENABLED` / `BRAIN_DREAM_SWEEP_DRY_RUN` (Task 3) et les rows `dream_runs` de phase `sweep` (Task 2).
- Produit : `KillswitchState.sweep_enabled: bool`, `.sweep_dry: bool`, `.sweep_clean_dry_nights: int`, et la ligne de briefing `- SWEEP  : …`.

- [ ] **Step 1 : écrire les tests qui échouent**

Dans `tests/unit/services/test_dream_run_service.py`, ajouter une méthode à la classe
`TestKillswitchState` existante (elle dispose de la fixture `session_factory` — SQLite en
mémoire — et du helper `_insert_run`) :

```python
    @pytest.mark.asyncio
    async def test_sweep_enabled_dry_from_the_drop_in(self, session_factory, tmp_path):
        """SWEEP suit exactement le contrat des autres phases optionnelles."""
        today = date.today()
        await _insert_run(
            session_factory, run_date=today, phase="sweep", phase_dry_run=True, model=None
        )
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
```

Dans `tests/unit/mcp/test_session_tools.py`, ajouter une méthode à la classe
`TestSectionKillswitches` existante (elle construit ses `KillswitchState` en direct) :

```python
    def test_sweep_row_sits_between_roadmap_and_graph(self):
        state = KillswitchState(
            last_run_date=date(2026, 8, 7),
            promote_enabled=False,
            promote_dry=False,
            reorg_enabled=False,
            reorg_dry=False,
            promote_clean_dry_nights=0,
            reorg_clean_dry_nights=0,
            sweep_enabled=True,
            sweep_dry=True,
            sweep_clean_dry_nights=3,
        )

        lines = _section_killswitches(state, graph_enabled=True).splitlines()

        assert "- SWEEP  : enabled (dry · 3 clean DRY nights)" in lines
        assert lines.index("- SWEEP  : enabled (dry · 3 clean DRY nights)") == (
            lines.index("- GRAPH:   enabled") - 1
        )
```

Dans `tests/unit/metrics/test_dream_metrics.py`, ajouter :

```python
def test_expected_phases_include_sweep_when_the_killswitch_is_open(tmp_path) -> None:
    from brain_v42.metrics.collector_dream import expected_dream_phases

    drop_in = tmp_path / "killswitches.conf"
    drop_in.write_text("[Service]\nEnvironment=BRAIN_DREAM_SWEEP_ENABLED=true\n")

    assert "sweep" in expected_dream_phases(drop_in)
```

- [ ] **Step 2 : vérifier l'échec**

```bash
unset VIRTUAL_ENV
uv run pytest tests/unit/services/test_dream_run_service.py tests/unit/mcp/test_session_tools.py \
              tests/unit/metrics/test_dream_metrics.py -v -k sweep
```
Attendu : `AttributeError: 'KillswitchState' object has no attribute 'sweep_enabled'` et l'absence de la ligne SWEEP.

- [ ] **Step 3 : ajouter les clés du drop-in**

Dans `src/brain_v42/dream_killswitches.py`, dans `_KS_KEYS`, après les entrées ROADMAP :

```python
    "BRAIN_DREAM_SWEEP_ENABLED": "sweep",
    "BRAIN_DREAM_SWEEP_DRY_RUN": "sweep_dry",
```

- [ ] **Step 4 : étendre `KillswitchState`**

Dans `src/brain_v42/services/dream_run_service.py`, à la fin des champs de `KillswitchState` :

```python
    sweep_enabled: bool = False
    sweep_dry: bool = True
    sweep_clean_dry_nights: int = 0
```

Puis dans `killswitch_state`, après le bloc `roadmap_*` (ligne 136) :

```python
            sweep_enabled = phase_enabled("sweep")
            sweep_dry = phase_dry("sweep", True)
            sweep_streak = await self._clean_dry_streak(session, "sweep")
```

et dans le `return KillswitchState(...)` final :

```python
            sweep_enabled=sweep_enabled,
            sweep_dry=sweep_dry,
            sweep_clean_dry_nights=sweep_streak,
```

- [ ] **Step 5 : ajouter la ligne de briefing**

Dans `src/brain_v42/mcp/tools/session_tools.py`, entre la ligne ROADMAP et la ligne GRAPH :

```python
    lines.append(
        _row("SWEEP  ", state.sweep_enabled, state.sweep_dry, state.sweep_clean_dry_nights)
    )
```

Mettre à jour le golden `tests/fixtures/briefing_full.md` — insérer entre `ROADMAP` et `GRAPH` :

```
- SWEEP  : disabled
```

- [ ] **Step 6 : ajouter la phase attendue côté métriques**

Dans `src/brain_v42/metrics/collector_dream.py`, ligne 45 :

```python
    return {phase for phase in ("promote", "reorg", "extract", "roadmap", "sweep") if flags.get(phase)}
```

- [ ] **Step 7 : vérifier que tout passe**

```bash
unset VIRTUAL_ENV
uv run pytest tests/unit/services/test_dream_run_service.py tests/unit/mcp/test_session_tools.py \
              tests/unit/metrics tests/unit/codex_gateway/test_killswitch_reader.py -q
GRAPH_LEDGER_WRITE_ENABLED=false BRAIN_V42_TEST_DB_URL=postgresql+asyncpg://brain:brain@localhost:5433/brain_test uv run pytest tests/integration/test_session_start_briefing.py -q
```
Attendu : unitaires verts, puis `3 passed` sur l'intégration — un compte non nul, pas « 3 skipped ». Le test golden du briefing échoue si l'étape 5 a oublié la fixture — c'est lui qui prouve que la ligne SWEEP est réellement rendue, et il ne prouve rien s'il skippe.

- [ ] **Step 8 : gates puis commit**

```bash
unset VIRTUAL_ENV
uv run ruff check src/ tests/ && uv run ruff format --check src/ tests/ && uv run mypy src/
git add src/brain_v42/dream_killswitches.py src/brain_v42/services/dream_run_service.py \
        src/brain_v42/mcp/tools/session_tools.py src/brain_v42/metrics/collector_dream.py \
        tests/fixtures/briefing_full.md tests/unit/
git commit -m "feat(briefing): exposer l'état du killswitch SWEEP au même titre que les autres phases"
```

---

### Task 5 : l'amendement doctrinal

La spec est explicite : sans cet amendement, le code contredit une interdiction écrite. « À écrire noir sur blanc, pas à contourner. » Trois documents portent l'interdiction, et un quatrième énoncé — celui de `docs/MCP_TOOLS.md` — devient piégeux sans précision, parce que `is_stale` (24 h) et le balayage (7 j) sont deux seuils différents.

**Files:**
- Modify: `CLAUDE.md:86-90` (l'exception stricte) et la section `## Configuration`
- Modify: `README.md:108-111`
- Modify: `docs/MCP_TOOLS.md:355`
- Test: `tests/unit/test_documentation_contract.py` (ajouter une fonction de test)

**Interfaces:**
- Consomme : la constante `auto_stale_7d` et les clés `BRAIN_DREAM_SWEEP_*` (Tasks 1 à 3).
- Produit : rien de programmatique — un contrat de documentation qui échoue si l'amendement est supprimé ou élargi.

- [ ] **Step 1 : écrire le test de contrat qui échoue**

Dans `tests/unit/test_documentation_contract.py`, ajouter à la fin :

```python
def test_server_side_sweep_amendment_is_narrow_and_stated() -> None:
    """L'exception du serveur doit être ÉCRITE, et bornée au serveur.

    Le CLAUDE.md interdisait catégoriquement toute fermeture automatique.
    La phase sweep contredirait cette phrase si elle n'était pas amendée
    explicitement : l'interdiction reste entière pour l'agent et le client,
    seul le serveur gagne le droit d'abandonner une session sans signe de vie.
    """
    claude_normalized = " ".join(CLAUDE.split())
    readme_normalized = " ".join(README.split())
    mcp_tools_normalized = " ".join(MCP_TOOLS.split())

    # L'interdiction survit, explicitement portée sur l'agent et le client.
    for document in (claude_normalized, readme_normalized):
        assert "ne ferme une session côté agent ou client" in document

    # L'exception est nommée, avec sa portée et sa constante.
    for document in (claude_normalized, readme_normalized):
        assert "sans signe de vie depuis 7 jours" in document
        assert "auto_stale_7d" in document
        assert "ne touche jamais le focus du projet" in document

    # Les deux seuils ne doivent pas pouvoir être confondus.
    assert "is_stale" in mcp_tools_normalized
    assert "24 hours old" in mcp_tools_normalized
    assert "seven-day server-side sweep" in mcp_tools_normalized

    # L'exception ne s'étend pas aux commandes du client.
    assert "restent des commandes explicites" in claude_normalized


def test_sweep_killswitches_are_documented_in_the_shared_configuration() -> None:
    claude_configuration = CLAUDE.split("## Configuration", maxsplit=1)[1]
    config_blocks = re.findall(r"```bash\n(.*?)```", claude_configuration, flags=re.DOTALL)
    shared_config = config_blocks[0]

    assert "BRAIN_DREAM_SWEEP_ENABLED=false" in shared_config
    assert "BRAIN_DREAM_SWEEP_DRY_RUN=true" in shared_config
    shared_key_list = _environment_assignment_keys(shared_config)
    assert len(shared_key_list) == len(set(shared_key_list))
```

- [ ] **Step 2 : vérifier l'échec**

```bash
unset VIRTUAL_ENV
uv run pytest tests/unit/test_documentation_contract.py -v -k sweep
```
Attendu : 2 failed sur les `assert ... in document`.

- [ ] **Step 3 : amender `CLAUDE.md`**

Remplacer le bloc de citation des lignes 86-90 par :

```markdown
> **Exception stricte — cycle de session :** appeler `brain_session_start`,
> `brain_session_list`, `brain_session_resume`, `brain_session_capture`,
> `brain_session_heartbeat`, `brain_session_end` ou `brain_session_abandon` uniquement
> après la commande explicite correspondante de
> l'utilisateur. Aucun hook, auto-close, livraison de travail ou fin de réponse
> ne ferme une session côté agent ou client. Une feature livrée peut mettre à jour la
> roadmap, jamais fermer Brain.
>
> **Seule exception, côté serveur :** la phase Dream `sweep` abandonne une session ouverte
> sans signe de vie depuis 7 jours, avec `abandonment_reason='auto_stale_7d'`. Elle n'écrit
> ni summary ni `next_focus` et ne touche jamais le focus du projet. Aucun agent, aucun hook
> et aucun client ne gagne ce droit : `start`, `resume`, `end` et `abandon` restent des
> commandes explicites de l'utilisateur.
```

Dans la section `## Configuration`, ajouter au premier bloc `bash`, après les clés ROADMAP :

```bash
# Sessions — balayage nocturne des fantômes (dream, serveur seul)
BRAIN_DREAM_SWEEP_ENABLED=false
BRAIN_DREAM_SWEEP_DRY_RUN=true
```

- [ ] **Step 4 : amender `README.md`**

Remplacer les lignes 108-111 par :

```markdown
L'utilisateur contrôle toutes les frontières de session. Les sept commandes ci-dessous
ne s'exécutent qu'après sa demande explicite. Aucun hook, auto-close, livraison de travail
ou fin de réponse ne ferme une session côté agent ou client.

Une seule exception, côté serveur : la phase Dream `sweep` abandonne une session ouverte
sans signe de vie depuis 7 jours, avec `abandonment_reason='auto_stale_7d'`. Elle ne produit
ni summary ni `next_focus` et ne touche jamais le focus du projet.
```

- [ ] **Step 5 : lever l'ambiguïté dans `docs/MCP_TOOLS.md`**

Remplacer la ligne 355 par :

```markdown
An open session becomes `is_stale=true` when its last heartbeat is at least 24 hours old. `status="stale"` selects that subset of open sessions; this derived flag never changes the persisted `status` and never auto-closes a session. The regular `open` filter therefore includes both fresh and stale open sessions. Do not confuse this 24-hour display flag with the separate seven-day server-side sweep, which is the only mechanism that moves an open session to `abandoned` without an explicit command (`abandonment_reason = 'auto_stale_7d'`).
```

- [ ] **Step 6 : vérifier le contrat complet de documentation**

```bash
unset VIRTUAL_ENV
uv run pytest tests/unit/test_documentation_contract.py -q
```
Attendu : tout vert. Ce fichier est susceptible d'échouer ailleurs pour des raisons sans rapport (contrat de migration) — dans ce cas, ne rien « réparer » au passage : le signaler et s'en tenir au périmètre.

- [ ] **Step 7 : commit**

```bash
git add CLAUDE.md README.md docs/MCP_TOOLS.md tests/unit/test_documentation_contract.py
git commit -m "docs(sessions): amender l'interdiction de fermeture automatique pour le seul serveur"
```

---

## Vérification finale, avant toute annonce de complétion

```bash
unset VIRTUAL_ENV
uv run ruff check src/ tests/ scripts/
uv run ruff format --check src/ tests/ scripts/
uv run mypy src/
uv run python scripts/check_module_layering.py --package src/brain_v42
uv run pytest tests/unit -q
GRAPH_LEDGER_WRITE_ENABLED=false BRAIN_V42_TEST_DB_URL=postgresql+asyncpg://brain:brain@localhost:5433/brain_test uv run pytest tests/integration -q
bash -n scripts/dream.sh
```

Attendu sur l'intégration : un compte `passed` non nul — mesuré `256 passed, 32 skipped` le
2026-08-07. Un total intégralement `skipped` n'est pas un vert, c'est une absence d'exécution.

`GRAPH_LEDGER_WRITE_ENABLED=false` n'est pas optionnel sur l'intégration : sans lui le `.env`
du tronc fuit et les tests sortent en ERROR au lieu d'échouer, ce qui masque les vraies
régressions (learning `54fdfddc`).

`BRAIN_V42_TEST_DB_URL` ne l'est pas davantage. `tests/integration/conftest.py` résout sa base
depuis cette variable seule et skippe toute la suite si elle est absente, ou si elle vise la base
de prod `brain` (garde `_resolve_integration_db_url`). Sans `BRAIN_V42_TEST_DB_URL`,
`pytest tests/integration` skippe la suite en totalité et sort en vert : exiger un compte
`passed` non nul, jamais « tout vert ». Mesure du 2026-08-07 : `288 skipped in 1.38s` sans la
variable, `256 passed, 32 skipped in 82.97s` avec. La base est `brain_test` du conteneur
`brain_v42_postgres` (port 5433), jamais `brain`.

Puis, avant commit final : `detect_changes()` pour vérifier que le rayon d'impact est celui
qu'on croit. L'index GitNexus est périmé — le rafraîchir **depuis la racine canonique**, jamais
depuis un worktree.

---

## Déploiement — actions opérateur, hors périmètre du code

Ces étapes ne sont pas des tâches de ce plan. Elles suivent la section « Sûreté et
déploiement » de la spec et demandent la main de l'opérateur.

1. **Armer en DRY.** Ajouter au drop-in `~/.config/systemd/user/brain-v42-dream.service.d/killswitches.conf` :
   ```
   # SWEEP: ouvert (dry) le <date> — soak avant tout flip WET (spec 2026-08-07).
   Environment=BRAIN_DREAM_SWEEP_ENABLED=true
   Environment=BRAIN_DREAM_SWEEP_DRY_RUN=true
   ```
   Puis `systemctl --user daemon-reload`. Le drop-in survit à la régénération de l'unité par
   `install.sh` — ne jamais mettre ces lignes dans le template (incident 2026-06-30).
2. **Laisser tourner plusieurs nuits.** Lire `logs/dream/<date>_sweep.log` et vérifier que
   la phase ne vise que des fantômes.
3. **Re-mesurer le fossé avant le flip WET.** Ne pas recopier les chiffres de la spec ni de
   ce plan :
   ```bash
   docker exec brain_v42_postgres psql -U brain -d brain -c \
     "select status, round(extract(epoch from now()-last_heartbeat_at)/86400.0,1) as age_days, project_key, client_key
        from brain_sessions where status='open' order by last_heartbeat_at desc;"
   ```
   Le flip n'est légitime que si un fossé net sépare encore les deux populations.
4. **Flip WET** : `Environment=BRAIN_DREAM_SWEEP_DRY_RUN=false`, `daemon-reload`.
5. **Vérifier après la première nuit WET** que les abandons portent bien `auto_stale_7d` et
   qu'aucune session vivante n'a été emportée.

L'abandon est **irréversible** (`brain_session_resume` exige `status='open'`). Trois
atténuations seulement : seuil généreux, DRY préalable, et conservation garantie des
captures. Pas d'`unabandon` tant que le DRY n'a produit aucun faux positif.

## Points explicitement laissés dehors

Repris de la spec, chacun pour sa raison :

- **Auto-heartbeat (`7ffe0e8a`)** — inutile ici (D3). Le principe de D1 le débloque
  conceptuellement : l'attribution par *(projet, acteur)* suffit.
- **Identité de session (`2dfbb83d`)** — mesurée non fonctionnelle, rendue non bloquante par D3.
- **Checkpoint sémantique (`d04dc588`)** — BLOCKED par son propre audit.
- **Doctrine « les subagents n'ouvrent pas de session » (D1)** — touche neuf projets, ticket
  écosystème séparé. Non applicable techniquement, et ce n'est pas un oubli.
- **Nettoyage manuel des 17 fantômes** — le DRY les listera, le WET les traitera. Les purger
  à la main d'ici là rendrait le succès invérifiable.
- **`unabandon`** — résoudrait un problème non observé.

## Limites connues de ce plan

- **Écart assumé avec la spec, sur un seul point.** La spec classe la frontière du prédicat
  (N−1 / N+1) en test *unitaire*. Le harnais unitaire du repository compile des statements
  contre des mocks : il ne peut pas évaluer un `WHERE`, donc un tel test prouverait la forme
  du SQL en laissant croire qu'il prouve la frontière. La frontière est donc testée en
  *intégration* (Task 1, Step 6), et l'unitaire garde ce qu'il peut réellement prouver : le
  cutoff calculé et la stricte inégalité. Les quatre comportements exigés par la spec sont
  couverts, deux ont changé d'étage.
- Le seuil de 7 jours reste calibré sur deux mesures (2026-08-06 et 2026-08-07) d'un même
  régime de travail. Des chantiers plus longs invalideraient la marge.
- `last_heartbeat_at` reste déclaratif : une session vivante mais silencieuse plus de 7 jours
  sera abandonnée à tort. C'est le compromis accepté en D3, atténué par la conservation des
  captures.
- La Task 4 (visibilité briefing/métriques) n'est pas exigée mot pour mot par la spec. Elle
  est incluse parce que sans elle, l'état armé/DRY de la phase n'est lisible nulle part et le
  soak de l'étape 2 du déploiement se pilote au grep. Elle est isolée dans sa propre tâche
  précisément pour rester coupable si l'opérateur juge qu'elle sort du périmètre.
