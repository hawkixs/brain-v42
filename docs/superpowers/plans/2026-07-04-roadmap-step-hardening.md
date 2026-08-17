# Roadmap Step Hardening — Implementation Plan

> **For agentic workers:** REQUIRED SUB-SKILL: Use superpowers:subagent-driven-development (recommended) or superpowers:executing-plans to implement this plan task-by-task. Steps use checkbox (`- [ ]`) syntax for tracking.

> Révision 2 (post-critique 3 juges) : flush stdout sous redirection (HIGH),
> refresh des doublons pour que le flip WET ne soit pas inerte (HIGH),
> dedup étendu aux `rejected` (MEDIUM ×2), tests RED de la boucle `_run`
> et du câblage rotation (MEDIUM ×2), lignes >100 chars corrigées (HIGH CI),
> accents dans les messages de commit, index [i/N] aussi sur les batches failed.

**Goal:** Durcir le step roadmap du dream (proposer-only) suite aux 6 findings de la
vérification du premier run dry (2026-07-04) : run à 597s/600s de budget, famine du
cap alphabétique (3/26 projets servis, 16 jamais scannés), 25 % de proposals no-op,
password PG en clair dans le log, doublons inter-nuits garantis, streak comptant les
rows au lieu des nuits.

**Architecture:** Le CLI `scripts/roadmap_curate.py` garde sa forme (batch par projet,
un appel LLM par projet, proposer-only). On y ajoute trois fonctions pures testables
(`drop_noops`, `rotate_keys`, `batch_allowance`) et on restructure la boucle propose
de `_run` en **persist incrémental** : chaque batch est validé/filtré/persisté dès
son retour LLM, avec une ligne de progression flushée par batch — un timeout ne perd
plus que le batch en cours, et le log reste diagnosticable. Le dedup inter-nuits
retourne les ids des doublons `proposed` (refresh) pour que le futur wet nocturne
applique aussi ce qui s'est accumulé en dry. Deux fixes ponctuels hors CLI :
`_clean_dry_streak` (DISTINCT run_date) et le log d'init engine (password masqué).
Le budget shell passe 10m→20m.

**Tech Stack:** Python 3.12+ (venv local = uv py3.14), SQLAlchemy 2.0 async (mode
`sa.text` + Table core dans scripts/), pytest + pytest-asyncio, structlog, bash
(dream.sh), ruff + mypy.

## Global Constraints

- **TDD obligatoire** : chaque étape de code suit Red → Green (test qui échoue AVANT l'implémentation). JAMAIS modifier un test existant pour faire passer du code — sauf changement de spec explicite (Task 7 : pin du timeout, précédent synth ≥15m).
- **Commits atomiques Conventional Commits**, messages en français AVEC accents, comme l'historique (`dédup`, `hermétique`…).
- **Vert avant chaque commit** : `env -u VIRTUAL_ENV uv run pytest tests/unit -q` + `env -u VIRTUAL_ENV uv run ruff check src/ tests/ scripts/` + `env -u VIRTUAL_ENV uv run ruff format --check src/ tests/ scripts/` + `env -u VIRTUAL_ENV uv run mypy src/`. Line-length ruff = 100 — passer `ruff format` sur les fichiers touchés avant le commit.
- **`env -u VIRTUAL_ENV`** systématique devant `uv run` : le shell peut hériter d'un VIRTUAL_ENV d'un autre projet (incident 2026-07-04 — uv sync du mauvais venv). Ne JAMAIS utiliser `uv run --active`.
- **mypy ne couvre PAS `scripts/`** (config projet) : dans les tests de scripts, suivre les conventions existantes de `tests/unit/test_roadmap_curate_apply.py` (`MagicMock(spec=AsyncSession)`, factories `@asynccontextmanager`).
- **Deux jumeaux `persist_proposals`** existent : `scripts/ticket_extract.py` ET `scripts/roadmap_curate.py` (squelette copié). Ce plan ne touche QUE celui de `roadmap_curate.py`. Ne pas éditer `ticket_extract.py`.
- **Tests pins à ne pas casser** : `tests/unit/test_dream_sh_roadmap.py` (mis à jour en Task 7 seulement), `tests/unit/test_dream_sh_phase_timeouts.py` (intouché — il ne pin que la PHASES array claude, pas le step roadmap).
- Blast radius (GitNexus, vérifié) : `persist_proposals` (roadmap) → `_run` + tests apply ; `_clean_dry_streak` → `killswitch_state` → briefing (golden tests d'intégration `tests/integration/test_session_start_briefing.py` — ne pas les casser ; ils tournent seulement si la DB de test est up).
- Ne PAS committer `AGENTS.md` / `CLAUDE.md` s'ils apparaissent modifiés (hors périmètre).

## File Structure

| Fichier | Rôle dans ce chantier |
|---|---|
| `src/brain_v42/services/dream_run_service.py` | Task 1 — streak DISTINCT run_date (méthode `_clean_dry_streak`, lignes ~118-137) |
| `tests/unit/services/test_dream_run_service.py` | Task 1 — nouveau test streak même-date |
| `src/brain_v42/db/engine.py` | Task 2 — masquage password ligne 48 |
| `tests/unit/db/test_engine.py` | Task 2 — test capture_logs |
| `scripts/roadmap_curate.py` | Tasks 3-6 — `drop_noops`, `PersistResult` + dedup, `rotate_keys`, `batch_allowance`, boucle `_run` incrémentale |
| `tests/unit/test_roadmap_curate.py` | Tasks 3, 5, 6 — tests fonctions pures + câblage rotation + boucle `_run` |
| `tests/unit/test_roadmap_curate_apply.py` | Task 4 — tests dedup persist (fichier des tests « apply/persist ») |
| `scripts/dream.sh` | Task 7 — `timeout 10m` → `timeout 20m` (ligne ~535) |
| `tests/unit/test_dream_sh_roadmap.py` | Task 7 — pin mis à jour |

Ordre d'exécution : 1 → 2 → 3 → 4 → 5 → 6 → 7. Les tasks 1 et 2 sont
indépendantes de tout. Les tasks 3-4 livrent des briques branchées a minima ;
la task 6 restructure la boucle en s'appuyant sur 3+4+5. La task 7 est du shell pur.

---

### Task 1: Streak clean-dry — compter les nuits distinctes

Le streak compte aujourd'hui les **rows** `done+dry` ; un run manuel le même jour
que la nightly compte donc comme une « nuit » de plus. Critère de flip WET faussé.
Fix : `COUNT(DISTINCT run_date)`.

**Files:**
- Modify: `src/brain_v42/services/dream_run_service.py:128-137` (méthode `DreamRunService._clean_dry_streak`)
- Test: `tests/unit/services/test_dream_run_service.py` (classe `TestKillswitchState`)

**Interfaces:**
- Consumes: rien (task feuille).
- Produces: `_clean_dry_streak` garde sa signature `(self, session, phase: str) -> int` — seule la sémantique du COUNT change (nuits distinctes).

- [ ] **Step 1: Écrire le test qui échoue**

Dans `tests/unit/services/test_dream_run_service.py`, ajouter à la classe
`TestKillswitchState` (après `test_roadmap_enabled_dry_with_streak`, ligne ~180),
en réutilisant le helper `_insert_run` du module (kwargs : `run_date`, `phase`,
`status`, `phase_dry_run`) :

```python
    @pytest.mark.asyncio
    async def test_streak_counts_distinct_nights_not_rows(self, session_factory):
        """Un re-run manuel le même jour ne doit PAS gonfler le streak (finding 2026-07-04)."""
        d = date.today()
        for _ in range(2):  # nightly + run manuel le même jour
            await _insert_run(
                session_factory, run_date=d, phase="roadmap", status="done", phase_dry_run=True
            )
        svc = DreamRunService(session_factory, table=_dream_runs)
        state = await svc.killswitch_state()
        assert state.roadmap_clean_dry_nights == 1
```

- [ ] **Step 2: Vérifier l'échec**

Run: `env -u VIRTUAL_ENV uv run pytest tests/unit/services/test_dream_run_service.py::TestKillswitchState::test_streak_counts_distinct_nights_not_rows -v`
Expected: FAIL — `assert 2 == 1` (le COUNT actuel compte les deux rows).

- [ ] **Step 3: Implémentation minimale**

Dans `src/brain_v42/services/dream_run_service.py`, méthode `_clean_dry_streak`,
remplacer :

```python
        stmt = (
            sa.select(sa.func.count())
            .select_from(t)
            .where(t.c.phase == phase)
            .where(t.c.status == "done")
            .where(t.c.phase_dry_run.is_(True))
        )
```

par :

```python
        # DISTINCT run_date : un re-run manuel le même jour n'est pas une
        # « nuit » de plus (finding vérification roadmap 2026-07-04).
        stmt = (
            sa.select(sa.func.count(sa.func.distinct(t.c.run_date)))
            .select_from(t)
            .where(t.c.phase == phase)
            .where(t.c.status == "done")
            .where(t.c.phase_dry_run.is_(True))
        )
```

- [ ] **Step 4: Vérifier le vert (test neuf + non-régression du module)**

Run: `env -u VIRTUAL_ENV uv run pytest tests/unit/services/test_dream_run_service.py -v`
Expected: PASS partout (les tests existants insèrent 1 row/date → inchangés).

- [ ] **Step 5: Gates + commit**

```bash
env -u VIRTUAL_ENV uv run ruff check src/brain_v42/services/dream_run_service.py tests/unit/services/test_dream_run_service.py
env -u VIRTUAL_ENV uv run ruff format --check src/brain_v42/services/dream_run_service.py tests/unit/services/test_dream_run_service.py
env -u VIRTUAL_ENV uv run mypy src/
git add src/brain_v42/services/dream_run_service.py tests/unit/services/test_dream_run_service.py
git commit -m "fix(dream): streak clean-dry compte les nuits distinctes, pas les rows"
```

---

### Task 2: Masquer le password PG dans le log d'init engine

`engine.py:48` logge `settings.postgres_url` brut → `postgresql+asyncpg://brain:brain@…`
atterrit dans `logs/dream/*_roadmap.log` (et partout ailleurs). Fix : logger l'URL
rendue par SQLAlchemy avec `hide_password=True`.

**Files:**
- Modify: `src/brain_v42/db/engine.py:48`
- Test: `tests/unit/db/test_engine.py`

**Interfaces:**
- Consumes: rien.
- Produces: rien (changement de contenu de log uniquement).

- [ ] **Step 1: Écrire le test qui échoue**

Dans `tests/unit/db/test_engine.py` (les fixtures autouse `reset_engine_singletons`
et `mock_settings` s'appliquent déjà), ajouter en fin de fichier :

```python
def test_engine_log_masks_password(mock_settings):
    """Le DSN loggé ne doit pas contenir le password (finding 2026-07-04)."""
    from structlog.testing import capture_logs

    mock_settings.postgres_url = "postgresql+asyncpg://brain:s3cret@localhost:5433/brain"
    from brain_v42.db.engine import get_engine

    with capture_logs() as logs:
        get_engine()

    created = [e for e in logs if e["event"] == "SQLAlchemy async engine created"]
    assert len(created) == 1
    assert "s3cret" not in created[0]["url"]
    assert "***" in created[0]["url"]
```

- [ ] **Step 2: Vérifier l'échec**

Run: `env -u VIRTUAL_ENV uv run pytest tests/unit/db/test_engine.py::test_engine_log_masks_password -v`
Expected: FAIL — `assert 's3cret' not in 'postgresql+asyncpg://brain:s3cret@…'`.

- [ ] **Step 3: Implémentation minimale**

Dans `src/brain_v42/db/engine.py`, remplacer la ligne 48 :

```python
        logger.info("SQLAlchemy async engine created", url=settings.postgres_url)
```

par :

```python
        logger.info(
            "SQLAlchemy async engine created",
            url=_engine.url.render_as_string(hide_password=True),
        )
```

(`AsyncEngine.url` est un `sqlalchemy.URL` ; `render_as_string(hide_password=True)`
rend `postgresql+asyncpg://brain:***@localhost:5433/brain`.)

- [ ] **Step 4: Vérifier le vert**

Run: `env -u VIRTUAL_ENV uv run pytest tests/unit/db/test_engine.py -v`
Expected: PASS partout.

- [ ] **Step 5: Gates + commit**

```bash
env -u VIRTUAL_ENV uv run ruff check src/brain_v42/db/engine.py tests/unit/db/test_engine.py
env -u VIRTUAL_ENV uv run ruff format --check src/brain_v42/db/engine.py tests/unit/db/test_engine.py
env -u VIRTUAL_ENV uv run mypy src/
git add src/brain_v42/db/engine.py tests/unit/db/test_engine.py
git commit -m "fix(db): masquer le password PG dans le log d'init engine"
```

---

### Task 3: `drop_noops` — écarter les proposals sans effet

Premier run réel : 8 proposals `status` deployed→deployed + 2 `rename` identiques au
nom courant = 25 % du cap brûlé. `parse_and_validate` valide la *forme* ; on ajoute un
filtre *d'effet* appliqué après validation, par batch, avec log du drop (jamais
silencieux). On ne lève PAS d'erreur (une erreur déclencherait le re-prompt correctif
LLM — gaspillage pour un no-op).

**Files:**
- Modify: `scripts/roadmap_curate.py` (nouvelle fonction après `parse_and_validate`, ligne ~221 ; branchement dans la boucle d'agrégation de `_run`, lignes ~676-688)
- Test: `tests/unit/test_roadmap_curate.py` (nouvelle classe `TestDropNoops`)

**Interfaces:**
- Consumes: `CurationDraft`, `ProjectBatch`, `FeatureCard` (dataclasses existantes du module).
- Produces: `drop_noops(drafts: list[CurationDraft], batch: ProjectBatch) -> tuple[list[CurationDraft], list[CurationDraft]]` — retourne `(kept, dropped)`. La Task 6 rebranche cet appel dans la boucle incrémentale.

- [ ] **Step 1: Écrire les tests qui échouent**

Dans `tests/unit/test_roadmap_curate.py`, ajouter la classe (compléter l'import
top-level `from scripts.roadmap_curate import …` avec `drop_noops` — et
`CurationDraft`, `FeatureCard`, `ProjectBatch`, `from uuid import UUID` s'ils
manquent) :

```python
class TestDropNoops:
    def _batch_one(self, *, name="Feature A", status="research", pinned=False):
        fid = UUID("11111111-1111-1111-1111-111111111111")
        return fid, ProjectBatch(
            project_key="p",
            features=[FeatureCard(id=fid, name=name, status=status, pinned=pinned)],
        )

    def test_status_identical_is_dropped(self):
        fid, batch = self._batch_one(status="deployed")
        drafts = [
            CurationDraft(
                op="status", feature_id=fid, payload={"status": "deployed"}, rationale="r"
            )
        ]
        kept, dropped = drop_noops(drafts, batch)
        assert kept == [] and len(dropped) == 1

    def test_status_different_is_kept(self):
        fid, batch = self._batch_one(status="research")
        drafts = [
            CurationDraft(op="status", feature_id=fid, payload={"status": "done"}, rationale="r")
        ]
        kept, dropped = drop_noops(drafts, batch)
        assert len(kept) == 1 and dropped == []

    def test_rename_identical_modulo_whitespace_is_dropped(self):
        fid, batch = self._batch_one(name="Feature A")
        drafts = [
            CurationDraft(
                op="rename", feature_id=fid, payload={"name": "  Feature A "}, rationale="r"
            )
        ]
        kept, dropped = drop_noops(drafts, batch)
        assert kept == [] and len(dropped) == 1

    def test_rename_different_is_kept(self):
        fid, batch = self._batch_one(name="Feature A")
        drafts = [
            CurationDraft(
                op="rename", feature_id=fid, payload={"name": "Feature B"}, rationale="r"
            )
        ]
        kept, dropped = drop_noops(drafts, batch)
        assert len(kept) == 1 and dropped == []

    def test_archive_and_merge_never_noop(self):
        fid, batch = self._batch_one()
        drafts = [
            CurationDraft(op="archive", feature_id=fid, payload={}, rationale="r"),
            CurationDraft(op="merge", feature_id=fid, payload={"into": str(fid)}, rationale="r"),
        ]
        kept, dropped = drop_noops(drafts, batch)
        assert len(kept) == 2 and dropped == []
```

- [ ] **Step 2: Vérifier l'échec**

Run: `env -u VIRTUAL_ENV uv run pytest tests/unit/test_roadmap_curate.py::TestDropNoops -v`
Expected: FAIL — `ImportError: cannot import name 'drop_noops'`.

- [ ] **Step 3: Implémentation minimale**

Dans `scripts/roadmap_curate.py`, juste après la fin de `parse_and_validate`
(après la ligne `return drafts`, ~221) :

```python
def drop_noops(
    drafts: list[CurationDraft], batch: ProjectBatch
) -> tuple[list[CurationDraft], list[CurationDraft]]:
    """Écarte les proposals sans effet — status identique, rename identique.

    Premier run réel (2026-07-04) : 10/40 proposals étaient des no-ops qui
    brûlaient le cap. Filtre d'effet post-validation ; on ne raise pas (un
    raise déclencherait le re-prompt correctif LLM pour un simple no-op).
    """
    by_id = {f.id: f for f in batch.features}
    kept: list[CurationDraft] = []
    dropped: list[CurationDraft] = []
    for draft in drafts:
        feature = by_id[draft.feature_id]
        is_noop = (
            draft.op == "status" and draft.payload.get("status") == feature.status
        ) or (
            draft.op == "rename"
            and str(draft.payload.get("name", "")).strip() == feature.name.strip()
        )
        (dropped if is_noop else kept).append(draft)
    return kept, dropped
```

- [ ] **Step 4: Vérifier le vert**

Run: `env -u VIRTUAL_ENV uv run pytest tests/unit/test_roadmap_curate.py -v`
Expected: PASS partout.

- [ ] **Step 5: Brancher dans `_run` (boucle d'agrégation actuelle)**

Dans `scripts/roadmap_curate.py`, boucle `for outcome in outcomes:` (~ligne 679),
remplacer :

```python
        if not outcome.drafts:
            skipped += 1
        all_drafts.extend(outcome.drafts)
```

par :

```python
        kept, noops = drop_noops(outcome.drafts, outcome.batch)
        if noops:
            print(f"~ projet {outcome.batch.project_key}: {len(noops)} no-op droppées")
        if not kept:
            skipped += 1
        all_drafts.extend(kept)
```

- [ ] **Step 6: Re-vérifier le vert complet**

Run: `env -u VIRTUAL_ENV uv run pytest tests/unit -q`
Expected: PASS (aucun test existant ne pin le comportement no-op).

- [ ] **Step 7: Gates + commit**

```bash
env -u VIRTUAL_ENV uv run ruff format scripts/roadmap_curate.py tests/unit/test_roadmap_curate.py
env -u VIRTUAL_ENV uv run ruff check scripts/roadmap_curate.py tests/unit/test_roadmap_curate.py
env -u VIRTUAL_ENV uv run ruff format --check scripts/roadmap_curate.py tests/unit/test_roadmap_curate.py
git add scripts/roadmap_curate.py tests/unit/test_roadmap_curate.py
git commit -m "feat(roadmap): drop des proposals no-op (status/rename sans effet)"
```

---

### Task 4: Dedup inter-nuits dans `persist_proposals` (roadmap)

En dry, les features ne bougent pas → chaque nuit ré-insère ~40 proposals quasi
identiques. Fix : avant chaque INSERT, chercher une row identique
(op + feature_id + payload, égalité JSONB sémantique) en statut `proposed` OU
`rejected` :

- doublon `proposed` → skip l'INSERT mais **retourner son id** (« refreshed ») —
  sans ça, le futur flip WET serait inerte : le wet n'applique que les ids du run,
  et le dedup empêcherait les proposals accumulées en dry d'y figurer ;
- doublon `rejected` → skip définitif (une proposal rejetée en review ne doit pas
  ressusciter à chaque cycle de rotation), compté pour le log.

Le retour devient un `PersistResult` (dataclass, style du module).

⚠️ **Uniquement `scripts/roadmap_curate.py`** — ne pas toucher le jumeau de
`scripts/ticket_extract.py`.

**Files:**
- Modify: `scripts/roadmap_curate.py:351-373` (fonction `persist_proposals` + nouvelle dataclass `PersistResult`) + call site dans `_run` (~ligne 699) + bloc wet (~ligne 708)
- Test: `tests/unit/test_roadmap_curate_apply.py` (classe `TestPersistProposals`)

**Interfaces:**
- Consumes: `CurationDraft`, table `roadmap_curation_proposals` (import function-local existant).
- Produces:
  ```python
  @dataclass
  class PersistResult:
      inserted: list[int]        # ids insérés ce run
      refreshed: list[int]       # ids des doublons 'proposed' re-proposés ce run
      rejected_skipped: int      # doublons 'rejected' écartés
  ```
  `persist_proposals(session_factory, drafts) -> PersistResult`. La Task 6 s'appuie
  sur ce retour ; le wet applique `inserted + refreshed`.

- [ ] **Step 1: Adapter le test existant + écrire les tests dedup (RED)**

Dans `tests/unit/test_roadmap_curate_apply.py` : ajouter `CurationDraft` et
`PersistResult` à l'import top-level existant
(`from scripts.roadmap_curate import apply_proposals, persist_proposals`), puis
dans la classe `TestPersistProposals` :

1. Adapter le test existant au nouveau retour (changement de spec assumé,
   même commit que l'implémentation) :

```python
    @pytest.mark.asyncio
    async def test_empty_drafts_noop(self):
        factory = MagicMock()
        res = await persist_proposals(factory, [])
        assert res.inserted == [] and res.refreshed == [] and res.rejected_skipped == 0
        factory.assert_not_called()
```

2. Ajouter (en réutilisant les helpers du module `_session_with` et le style
   `MagicMock(spec=AsyncSession)` ; les résultats du SELECT dedup exposent
   `.first()`) :

```python
    @pytest.mark.asyncio
    async def test_duplicate_proposed_is_refreshed_not_reinserted(self):
        """Doublon 'proposed' → pas d'INSERT, id retourné en refreshed (flip WET non inerte)."""
        draft = CurationDraft(op="archive", feature_id=uuid4(), payload={}, rationale="r")
        dup_found = MagicMock()
        dup_found.first = MagicMock(return_value=(123, "proposed"))
        factory, session = _session_with([dup_found])
        res = await persist_proposals(factory, [draft])
        assert res.inserted == [] and res.refreshed == [123] and res.rejected_skipped == 0
        assert session.execute.await_count == 1  # SELECT seulement, pas d'INSERT

    @pytest.mark.asyncio
    async def test_duplicate_rejected_is_skipped_for_good(self):
        """Doublon 'rejected' → ni INSERT ni refresh — pas de résurrection en review."""
        draft = CurationDraft(op="archive", feature_id=uuid4(), payload={}, rationale="r")
        dup_found = MagicMock()
        dup_found.first = MagicMock(return_value=(55, "rejected"))
        factory, session = _session_with([dup_found])
        res = await persist_proposals(factory, [draft])
        assert res.inserted == [] and res.refreshed == [] and res.rejected_skipped == 1
        assert session.execute.await_count == 1

    @pytest.mark.asyncio
    async def test_new_draft_is_inserted(self):
        """Pas de doublon → SELECT (None) puis INSERT (returning id)."""
        draft = CurationDraft(op="archive", feature_id=uuid4(), payload={}, rationale="r")
        dup_none = MagicMock()
        dup_none.first = MagicMock(return_value=None)
        factory, session = _session_with([dup_none, _scalar_one(7)])
        res = await persist_proposals(factory, [draft])
        assert res.inserted == [7] and res.refreshed == [] and res.rejected_skipped == 0
        assert session.execute.await_count == 2  # SELECT + INSERT
```

(Vérifier la forme exacte de `_session_with` / `_scalar_one` en tête du fichier —
`_scalar_one(7)` doit fournir `.scalar_one()` ; si le helper diffère, construire le
mock inline dans le même style.)

- [ ] **Step 2: Vérifier l'échec**

Run: `env -u VIRTUAL_ENV uv run pytest "tests/unit/test_roadmap_curate_apply.py::TestPersistProposals" -v`
Expected: FAIL — `ImportError: cannot import name 'PersistResult'`.

- [ ] **Step 3: Implémentation**

Dans `scripts/roadmap_curate.py` :

1. Dataclass, à placer avec les autres (`BatchOutcome`, ~ligne 118) :

```python
@dataclass
class PersistResult:
    """Résultat de persist_proposals — voir dedup inter-nuits (2026-07-04)."""

    inserted: list[int] = field(default_factory=list)
    refreshed: list[int] = field(default_factory=list)
    rejected_skipped: int = 0
```

2. Remplacer `persist_proposals` en entier :

```python
async def persist_proposals(session_factory: Any, drafts: list[CurationDraft]) -> PersistResult:
    """INSERT proposals status='proposed', en dédupliquant contre l'existant.

    Dedup inter-nuits (finding 2026-07-04) : en dry les features ne bougent
    pas, chaque nuit re-proposerait les mêmes ops. Une row identique
    (op + feature_id + payload, égalité JSONB sémantique) suffit à skipper :
    'proposed' → refresh (l'id est retourné, le wet du run l'applique) ;
    'rejected' → skip définitif (pas de résurrection en review).
    """
    from brain_v42.db.tables import roadmap_curation_proposals  # noqa: PLC0415

    result = PersistResult()
    if not drafts:
        return result
    t = roadmap_curation_proposals
    async with session_factory() as session:
        async with session.begin():
            for draft in drafts:
                dup_stmt = (
                    sa.select(t.c.id, t.c.status)
                    .where(
                        t.c.op == draft.op,
                        t.c.feature_id == draft.feature_id,
                        t.c.payload == draft.payload,
                        t.c.status.in_(("proposed", "rejected")),
                    )
                    # asc : 'proposed' < 'rejected' — si les deux existent,
                    # le refresh gagne sur le skip définitif.
                    .order_by(t.c.status)
                    .limit(1)
                )
                row = (await session.execute(dup_stmt)).first()
                if row is not None:
                    dup_id, dup_status = row
                    if dup_status == "proposed":
                        result.refreshed.append(dup_id)
                    else:
                        result.rejected_skipped += 1
                    continue
                stmt = (
                    t.insert()
                    .values(
                        op=draft.op,
                        feature_id=draft.feature_id,
                        payload=draft.payload,
                        rationale=draft.rationale,
                        status="proposed",
                    )
                    .returning(t.c.id)
                )
                result.inserted.append((await session.execute(stmt)).scalar_one())
    return result
```

3. Adapter le call site dans `_run` (~ligne 699) — remplacer :

```python
    proposal_ids = await persist_proposals(sf, all_drafts)
```

par :

```python
    res = await persist_proposals(sf, all_drafts)
    proposal_ids = res.inserted
    if res.refreshed:
        print(f"~ {len(res.refreshed)} doublons déjà proposés — refresh (dédup inter-nuits)")
    if res.rejected_skipped:
        print(f"~ {res.rejected_skipped} déjà rejetées — non ré-insérées")
```

4. Adapter le bloc wet (~ligne 708) — le wet applique insérées + rafraîchies :

```python
    # --wet: apply du run (insérées + rafraîchies — sans les rafraîchies, le
    # dedup rendrait le flip WET inerte). Restreint aux ops sûres.
    if args.wet and (proposal_ids or res.refreshed):
        applied = await apply_proposals(
            sf, proposal_ids + res.refreshed, allowed_ops=WET_APPLYABLE_OPS
        )
        print(f"wet: {applied} appliqués (ops {WET_APPLYABLE_OPS})")
```

- [ ] **Step 4: Vérifier le vert**

Run: `env -u VIRTUAL_ENV uv run pytest tests/unit/test_roadmap_curate_apply.py tests/unit/test_roadmap_curate.py -v`
Expected: PASS partout.

- [ ] **Step 5: Gates + commit**

```bash
env -u VIRTUAL_ENV uv run ruff format scripts/roadmap_curate.py tests/unit/test_roadmap_curate_apply.py
env -u VIRTUAL_ENV uv run ruff check scripts/roadmap_curate.py tests/unit/test_roadmap_curate_apply.py
env -u VIRTUAL_ENV uv run ruff format --check scripts/roadmap_curate.py tests/unit/test_roadmap_curate_apply.py
env -u VIRTUAL_ENV uv run pytest tests/unit -q
git add scripts/roadmap_curate.py tests/unit/test_roadmap_curate_apply.py
git commit -m "feat(roadmap): dédup inter-nuits — refresh des proposed, skip des rejected"
```

---

### Task 5: Rotation déterministe des projets scannés

`_KEYS_SQL` fait `ORDER BY project_key LIMIT 10` : les 10 premiers projets
alphabétiques sont scannés chaque nuit, les 16 autres jamais. Fix : fenêtre
glissante déterministe par jour — on récupère TOUTES les clés, on tourne de
`limit` positions par jour (`toordinal()`), cycle complet en ⌈n/limit⌉ nuits.

**Files:**
- Modify: `scripts/roadmap_curate.py` (`_KEYS_SQL` ~ligne 226, `fetch_project_batches` ~ligne 265, nouvelle fonction `rotate_keys`)
- Test: `tests/unit/test_roadmap_curate.py` (classes `TestRotateKeys` + `TestFetchRotationWiring`)

**Interfaces:**
- Consumes: rien.
- Produces: `rotate_keys(keys: list[str], limit: int, day_ordinal: int) -> list[str]` ; `fetch_project_batches(session_factory, limit, day_ordinal: int | None = None)` — signature étendue, rétro-compatible (None → `date.today().toordinal()`).

- [ ] **Step 1: Écrire les tests qui échouent**

Dans `tests/unit/test_roadmap_curate.py` (compléter l'import top-level avec
`rotate_keys` et `fetch_project_batches` ; il faut aussi `AsyncMock`, `MagicMock`,
`asynccontextmanager`, `AsyncSession` — vérifier ce que le fichier importe déjà) :

```python
class TestRotateKeys:
    def test_window_advances_by_limit_each_day(self):
        keys = [f"p{i:02d}" for i in range(26)]
        day0 = rotate_keys(keys, 10, day_ordinal=0)
        day1 = rotate_keys(keys, 10, day_ordinal=1)
        day2 = rotate_keys(keys, 10, day_ordinal=2)
        assert day0 == keys[0:10]
        assert day1 == keys[10:20]
        assert day2 == keys[20:26] + keys[0:4]  # wrap

    def test_full_cycle_covers_every_project(self):
        keys = [f"p{i:02d}" for i in range(26)]
        seen: set[str] = set()
        for day in range(3):  # ceil(26/10) = 3 nuits
            seen.update(rotate_keys(keys, 10, day_ordinal=day))
        assert seen == set(keys)

    def test_fewer_projects_than_limit_returns_all(self):
        keys = ["a", "b", "c"]
        assert sorted(rotate_keys(keys, 10, day_ordinal=5)) == keys
        assert len(rotate_keys(keys, 10, day_ordinal=5)) == 3

    def test_empty_keys(self):
        assert rotate_keys([], 10, day_ordinal=3) == []

    def test_deterministic_same_day(self):
        keys = [f"p{i}" for i in range(26)]
        assert rotate_keys(keys, 10, 7) == rotate_keys(keys, 10, 7)


class TestFetchRotationWiring:
    @pytest.mark.asyncio
    async def test_fetch_queries_only_rotated_window(self):
        """fetch_project_batches n'interroge que les projets de la fenêtre rotée."""
        keys_result = MagicMock()
        keys_result.all = MagicMock(return_value=[("a",), ("b",), ("c",)])
        empty_features = MagicMock()
        empty_features.mappings = MagicMock(
            return_value=MagicMock(all=MagicMock(return_value=[]))
        )
        session = MagicMock(spec=AsyncSession)
        session.execute = AsyncMock(side_effect=[keys_result, empty_features, empty_features])

        @asynccontextmanager
        async def factory():
            yield session

        batches = await fetch_project_batches(factory, limit=2, day_ordinal=1)
        assert batches == []  # features vides → batchs skippés
        # offset = (1*2) % 3 = 2 → fenêtre rotée = ['c', 'a']
        feature_calls = session.execute.await_args_list[1:]
        assert [call.args[1]["pk"] for call in feature_calls] == ["c", "a"]
```

- [ ] **Step 2: Vérifier l'échec**

Run: `env -u VIRTUAL_ENV uv run pytest tests/unit/test_roadmap_curate.py::TestRotateKeys tests/unit/test_roadmap_curate.py::TestFetchRotationWiring -v`
Expected: FAIL — `ImportError: cannot import name 'rotate_keys'`.

- [ ] **Step 3: Implémentation**

Dans `scripts/roadmap_curate.py` :

1. `_KEYS_SQL` perd sa LIMIT (remplacer la constante entière) :

```python
_KEYS_SQL = """
SELECT DISTINCT project_key FROM features
WHERE status NOT IN ('done', 'archived') AND merged_into IS NULL
ORDER BY project_key
"""
```

2. Nouvelle fonction pure juste au-dessus de `fetch_project_batches` :

```python
def rotate_keys(keys: list[str], limit: int, day_ordinal: int) -> list[str]:
    """Fenêtre glissante déterministe sur la liste (triée) des projets.

    Avance de `limit` positions par jour → cycle complet en ⌈n/limit⌉
    nuits, à liste stable ; si elle change entre nuits la couverture
    reste bornée (l'offset avance quand même chaque jour). Sans
    rotation, ORDER BY + LIMIT scannait les 10 premiers projets
    alphabétiques chaque nuit et jamais les 16 autres (2026-07-04).
    """
    if not keys:
        return []
    offset = (day_ordinal * limit) % len(keys)
    rotated = keys[offset:] + keys[:offset]
    return rotated[:limit]
```

3. `fetch_project_batches` — nouvelle signature et sélection des clés
(remplacer la signature et la ligne `keys = …`) :

```python
async def fetch_project_batches(
    session_factory: Any, limit: int, day_ordinal: int | None = None
) -> list[ProjectBatch]:
    """Batchs par projet : features vivantes (cap 30) + digests (cap 10/feature).

    La fenêtre de projets tourne chaque jour (rotate_keys) pour que tous
    les projets soient couverts en ⌈n/limit⌉ nuits.
    """
    if day_ordinal is None:
        day_ordinal = date.today().toordinal()
    async with session_factory() as session:
        all_keys = [r[0] for r in (await session.execute(sa.text(_KEYS_SQL))).all()]
        keys = rotate_keys(all_keys, limit, day_ordinal)
```

(le reste du corps — boucle `for pk in keys:` — est inchangé ; le paramètre
`{"lim": limit}` de l'ancien execute disparaît avec la LIMIT).

- [ ] **Step 4: Vérifier le vert**

Run: `env -u VIRTUAL_ENV uv run pytest tests/unit/test_roadmap_curate.py tests/unit/test_roadmap_curate_apply.py -v`
Expected: PASS partout.

- [ ] **Step 5: Gates + commit**

```bash
env -u VIRTUAL_ENV uv run ruff format scripts/roadmap_curate.py tests/unit/test_roadmap_curate.py
env -u VIRTUAL_ENV uv run ruff check scripts/roadmap_curate.py tests/unit/test_roadmap_curate.py
env -u VIRTUAL_ENV uv run ruff format --check scripts/roadmap_curate.py tests/unit/test_roadmap_curate.py
env -u VIRTUAL_ENV uv run pytest tests/unit -q
git add scripts/roadmap_curate.py tests/unit/test_roadmap_curate.py
git commit -m "feat(roadmap): rotation déterministe des projets scannés par nuit"
```

---

### Task 6: Fair-share du cap + persist incrémental + progression par batch

Le cœur du durcissement. Aujourd'hui : tous les appels LLM d'abord, troncature
globale `[:40]` en ordre alphabétique (3/26 projets servis), persist unique à la
fin (un timeout perd TOUT), zéro log de progression (un timeout laisse un log
vide). Après : une seule boucle — chaque batch est curé, filtré (no-ops), plafonné
à sa part équitable du cap restant, persisté (dedup), loggé avec timing **et
`flush=True`** (stdout est block-bufferisé sous la redirection `>>` de dream.sh —
sans flush, un SIGTERM du timeout perdrait toutes les lignes de progression, le
finding resterait raté pile dans le cas visé). Le cap épuisé interrompt la boucle
(plus d'appels LLM inutiles).

**Files:**
- Modify: `scripts/roadmap_curate.py` — nouvelle fonction `batch_allowance` + restructuration du bloc propose de `_run` (lignes ~652-717)
- Test: `tests/unit/test_roadmap_curate.py` (classes `TestBatchAllowance` + `TestRunProposeLoop`)

**Interfaces:**
- Consumes: `drop_noops` (Task 3), `persist_proposals -> PersistResult` (Task 4), `fetch_project_batches(sf, limit, day_ordinal)` (Task 5), `curate_batch`, `record_dream_run`, `MAX_PROPOSALS_PER_NIGHT`.
- Produces: `batch_allowance(remaining_cap: int, remaining_batches: int) -> int` ; format de log par batch `[i/N] <project>: …` flushé (le morning-check lira ces lignes — les batches failed gardent leur index `! [i/N] … failed:`).

- [ ] **Step 1: Écrire les tests qui échouent (fonction pure)**

Dans `tests/unit/test_roadmap_curate.py` (ajouter `batch_allowance` à l'import) :

```python
class TestBatchAllowance:
    def test_even_split(self):
        assert batch_allowance(40, 10) == 4

    def test_ceil_redistributes(self):
        assert batch_allowance(38, 9) == 5  # ceil — les slots non consommés se redistribuent

    def test_last_batch_gets_all_remaining(self):
        assert batch_allowance(7, 1) == 7

    def test_exhausted_cap(self):
        assert batch_allowance(0, 5) == 0

    def test_no_remaining_batches(self):
        assert batch_allowance(10, 0) == 0
```

- [ ] **Step 2: Vérifier l'échec**

Run: `env -u VIRTUAL_ENV uv run pytest tests/unit/test_roadmap_curate.py::TestBatchAllowance -v`
Expected: FAIL — `ImportError: cannot import name 'batch_allowance'`.

- [ ] **Step 3: Implémenter `batch_allowance`**

Dans `scripts/roadmap_curate.py`, sous `rotate_keys` :

```python
def batch_allowance(remaining_cap: int, remaining_batches: int) -> int:
    """Part équitable du cap restant pour le prochain batch (ceil).

    Le ceil redistribue les slots non consommés par les batches
    précédents. Sans fair-share, la troncature globale [:cap] en ordre
    de batch servait 3 projets sur 26 (finding 2026-07-04).
    """
    if remaining_batches <= 0 or remaining_cap <= 0:
        return 0
    return -(-remaining_cap // remaining_batches)
```

Run: `env -u VIRTUAL_ENV uv run pytest tests/unit/test_roadmap_curate.py::TestBatchAllowance -v` → PASS.

- [ ] **Step 4: Écrire les tests RED de la boucle `_run`**

Toujours dans `tests/unit/test_roadmap_curate.py` — la boucle restructurée est le
cœur du chantier, elle a ses propres tests (collaborateurs monkeypatchés ; les
imports `Settings`/`get_session_factory` de `_run` sont function-local, on
monkeypatche donc leurs modules d'origine). Ajouter :

```python
class TestRunProposeLoop:
    """Flux propose de _run — collaborateurs monkeypatchés, aucun I/O réel."""

    def _args(self, limit=10, wet=False):
        return SimpleNamespace(limit=limit, wet=wet, apply_ids=None, model=None, base_url=None)

    def _feature(self, fid):
        return FeatureCard(id=fid, name="F", status="research", pinned=False)

    def _outcome(self, batch, fid):
        draft = CurationDraft(op="archive", feature_id=fid, payload={}, rationale="r")
        return BatchOutcome(batch=batch, drafts=[draft])

    def _hermetic(self, monkeypatch):
        monkeypatch.setattr("brain_v42.config.Settings", MagicMock())
        monkeypatch.setattr(
            "brain_v42.db.engine.get_session_factory", MagicMock(return_value=MagicMock())
        )
        import scripts.roadmap_curate as rc

        monkeypatch.setattr(rc, "record_dream_run", AsyncMock())
        return rc

    @pytest.mark.asyncio
    async def test_persist_called_per_batch_and_progress_flushed(self, monkeypatch, capsys):
        """Persist incrémental : un persist PAR batch + ligne [i/N] par batch."""
        rc = self._hermetic(monkeypatch)
        fid1, fid2 = uuid4(), uuid4()
        b1 = ProjectBatch(project_key="p1", features=[self._feature(fid1)])
        b2 = ProjectBatch(project_key="p2", features=[self._feature(fid2)])
        monkeypatch.setattr(rc, "fetch_project_batches", AsyncMock(return_value=[b1, b2]))
        monkeypatch.setattr(
            rc,
            "curate_batch",
            AsyncMock(side_effect=[self._outcome(b1, fid1), self._outcome(b2, fid2)]),
        )
        persist = AsyncMock(
            side_effect=[rc.PersistResult(inserted=[1]), rc.PersistResult(inserted=[2])]
        )
        monkeypatch.setattr(rc, "persist_proposals", persist)

        exit_code = await rc._run(self._args(), "key", "model", "https://api.test")

        assert exit_code == 0
        assert persist.await_count == 2  # incrémental — l'ancien design persistait 1 fois
        out = capsys.readouterr().out
        assert "[1/2] p1:" in out and "[2/2] p2:" in out

    @pytest.mark.asyncio
    async def test_cap_exhausted_skips_remaining_llm_calls(self, monkeypatch, capsys):
        """Cap épuisé → break AVANT l'appel LLM suivant, message explicite."""
        rc = self._hermetic(monkeypatch)
        monkeypatch.setattr(rc, "MAX_PROPOSALS_PER_NIGHT", 1)
        fid1, fid2 = uuid4(), uuid4()
        b1 = ProjectBatch(project_key="p1", features=[self._feature(fid1)])
        b2 = ProjectBatch(project_key="p2", features=[self._feature(fid2)])
        monkeypatch.setattr(rc, "fetch_project_batches", AsyncMock(return_value=[b1, b2]))
        curate = AsyncMock(return_value=self._outcome(b1, fid1))
        monkeypatch.setattr(rc, "curate_batch", curate)
        monkeypatch.setattr(
            rc, "persist_proposals", AsyncMock(return_value=rc.PersistResult(inserted=[1]))
        )

        exit_code = await rc._run(self._args(), "key", "model", "https://api.test")

        assert exit_code == 0
        assert curate.await_count == 1  # le batch 2 n'est jamais envoyé au LLM
        assert "épuisé" in capsys.readouterr().out
```

Compléter les imports du fichier : `from types import SimpleNamespace`,
`from unittest.mock import AsyncMock, MagicMock`, `from uuid import UUID, uuid4`,
et `BatchOutcome` + `PersistResult` dans l'import `from scripts.roadmap_curate
import …` (selon l'existant).

Run: `env -u VIRTUAL_ENV uv run pytest tests/unit/test_roadmap_curate.py::TestRunProposeLoop -v`
Expected: FAIL — l'ancien `_run` persiste une seule fois (`await_count == 1`) et
n'émet aucune ligne `[i/N]`.

- [ ] **Step 5: Restructurer le bloc propose de `_run`**

Dans `scripts/roadmap_curate.py`, remplacer TOUT le bloc depuis
`# Propose mode (dry ou wet).` jusqu'à `return 1 if any_failed else 0` (fin de
`_run`) par :

```python
    # Propose mode (dry ou wet) — persist incrémental batch par batch :
    # un timeout shell ne perd que le batch en cours, et le log de
    # progression par batch (flush=True : stdout est block-bufferisé
    # sous la redirection >> de dream.sh) rend la nuit diagnosticable
    # (finding 2026-07-04 : 597s/600s, persist unique final, log vide).
    # NB : un SIGTERM en plein batch laisse la nuit sans row dream_runs
    # (record_dream_run est en fin de run) — mitigé par le budget 20m.
    batches = await fetch_project_batches(sf, args.limit)
    if not batches:
        print("Aucune feature vivante — rien à curer.", flush=True)
        await record_dream_run(
            sf, "done", dry=not args.wet, duration_s=time.monotonic() - t0, error=None
        )
        return 0

    http_client = httpx.AsyncClient(
        base_url=base_url,
        headers={"Authorization": f"Bearer {api_key}"},
        timeout=httpx.Timeout(connect=10.0, read=180.0, write=30.0, pool=10.0),
    )

    all_ids: list[int] = []
    refreshed_ids: list[int] = []
    remaining_cap = MAX_PROPOSALS_PER_NIGHT
    scanned = 0
    skipped = 0
    failed = 0
    total = len(batches)
    try:
        for i, batch in enumerate(batches, 1):
            if remaining_cap <= 0:
                print(
                    f"! cap {MAX_PROPOSALS_PER_NIGHT} proposals/nuit épuisé — "
                    f"{total - i + 1} projets non traités ce soir "
                    f"(le cycle de rotation les resservira)",
                    flush=True,
                )
                break
            t_batch = time.monotonic()
            outcome = await curate_batch(http_client, model, batch)
            scanned += 1
            if outcome.failed:
                failed += 1
                any_failed = True
                error_msg = outcome.error
                print(f"! [{i}/{total}] {batch.project_key} failed: {outcome.error}", flush=True)
                continue
            kept, noops = drop_noops(outcome.drafts, batch)
            allowance = batch_allowance(remaining_cap, total - i + 1)
            to_persist, cap_dropped = kept[:allowance], kept[allowance:]
            res = await persist_proposals(sf, to_persist)
            remaining_cap -= len(res.inserted)
            all_ids.extend(res.inserted)
            refreshed_ids.extend(res.refreshed)
            if not res.inserted and not res.refreshed:
                skipped += 1
            if cap_dropped:
                print(
                    f"! projet {batch.project_key}: {len(cap_dropped)} proposals "
                    f"au-delà de la part de cap ({allowance}) — droppées "
                    f"(pas de troncature silencieuse)",
                    flush=True,
                )
            print(
                f"[{i}/{total}] {batch.project_key}: "
                f"{len(outcome.drafts)} drafts, {len(noops)} no-op, "
                f"{len(res.refreshed)} dup, {res.rejected_skipped} rej-skip, "
                f"{len(cap_dropped)} cap-drop, {len(res.inserted)} persistées "
                f"({time.monotonic() - t_batch:.0f}s)",
                flush=True,
            )
    finally:
        await http_client.aclose()

    print(
        f"{scanned} projets scannés, {len(all_ids)} proposals, "
        f"{skipped} sans proposition, {failed} failed",
        flush=True,
    )
    if all_ids:
        print(f"proposal ids: {all_ids}", flush=True)
    if refreshed_ids:
        print(f"déjà proposées (refresh): {refreshed_ids}", flush=True)

    # --wet: apply du run (insérées + rafraîchies — sans les rafraîchies, le
    # dedup rendrait le flip WET inerte). Restreint aux ops sûres. JAMAIS
    # merge/rename.
    if args.wet and (all_ids or refreshed_ids):
        applied = await apply_proposals(sf, all_ids + refreshed_ids, allowed_ops=WET_APPLYABLE_OPS)
        print(f"wet: {applied} appliqués (ops {WET_APPLYABLE_OPS})", flush=True)

    duration = time.monotonic() - t0
    status = "fail" if any_failed else "done"
    await record_dream_run(
        sf, status=status, dry=not args.wet, duration_s=duration, error=error_msg
    )
    return 1 if any_failed else 0
```

Notes d'implémentation :
- Le bloc wet de la Task 4 (variables `res`/`proposal_ids`) disparaît avec cette
  restructuration — c'est attendu (double-touch géré : chaque commit reste vert).
- La ligne résumé finale garde EXACTEMENT sa forme (`N projets scannés, …`) — le
  morning-check et d'éventuels greps s'y attendent.
- Trade-off assumé : la part de cap est appliquée AVANT le dedup — un batch plein
  de doublons persiste moins que sa part et le reliquat se redistribue via
  `remaining_cap` (le ceil de `batch_allowance` s'en charge).
- `skipped` compte les projets sans proposal persistée NI rafraîchie — sémantique
  du résumé « sans proposition » préservée.

- [ ] **Step 6: Vérifier le vert complet**

Run: `env -u VIRTUAL_ENV uv run pytest tests/unit -q`
Expected: PASS — dont `TestRunProposeLoop` (RED au Step 4, GREEN maintenant).

- [ ] **Step 7: Smoke test à blanc du CLI (sans LLM, sans DB)**

Run: `env -u VIRTUAL_ENV uv run python -m scripts.roadmap_curate --limit 0 2>&1 | tail -1`
Expected: `roadmap_curate: error: argument --limit: doit être >= 1 (reçu : 0)` —
le module s'importe et parse (pas de SyntaxError/NameError introduits).

- [ ] **Step 8: Gates + commit**

```bash
env -u VIRTUAL_ENV uv run ruff format scripts/roadmap_curate.py tests/unit/test_roadmap_curate.py
env -u VIRTUAL_ENV uv run ruff check scripts/roadmap_curate.py tests/unit/test_roadmap_curate.py
env -u VIRTUAL_ENV uv run ruff format --check scripts/roadmap_curate.py tests/unit/test_roadmap_curate.py
env -u VIRTUAL_ENV uv run pytest tests/unit -q
git add scripts/roadmap_curate.py tests/unit/test_roadmap_curate.py
git commit -m "feat(roadmap): fair-share du cap + persist incrémental + progression par batch"
```

---

### Task 7: Budget shell roadmap 10m → 20m

Premier run réel : 597s pour 10 projets sous `timeout 10m` — 3 s de marge. Même
archétype que les timeouts synth (bump 10→15 le 2026-05-03). Le persist incrémental
(Task 6) rend le timeout non catastrophique, mais le budget doit quand même laisser
de la marge : 20m = ~100 % de headroom sur l'observé. Changement de spec pin :
test d'abord (RED), puis dream.sh (GREEN).

**Files:**
- Modify: `tests/unit/test_dream_sh_roadmap.py` (fonction `test_roadmap_step_has_timeout_and_own_log`)
- Modify: `scripts/dream.sh` (~ligne 535)

**Interfaces:**
- Consumes: rien.
- Produces: rien (config shell).

- [ ] **Step 1: Mettre à jour le pin (RED)**

Dans `tests/unit/test_dream_sh_roadmap.py`, remplacer :

```python
def test_roadmap_step_has_timeout_and_own_log():
    content = _content()
    assert "timeout 10m uv run python -m scripts.roadmap_curate" in content
    assert "_roadmap.log" in content
```

par :

```python
def test_roadmap_step_has_timeout_and_own_log():
    """Budget 20m : premier run réel à 597s/600s (2026-07-04) — même
    archétype que les timeouts synth (bump 10→15 du 2026-05-03)."""
    content = _content()
    assert "timeout 20m uv run python -m scripts.roadmap_curate" in content
    assert "_roadmap.log" in content
```

- [ ] **Step 2: Vérifier l'échec**

Run: `env -u VIRTUAL_ENV uv run pytest tests/unit/test_dream_sh_roadmap.py -v`
Expected: FAIL — `test_roadmap_step_has_timeout_and_own_log` (dream.sh contient encore `timeout 10m`).

- [ ] **Step 3: Bump dans dream.sh (GREEN)**

Dans `scripts/dream.sh` (~ligne 535), remplacer :

```bash
  timeout 10m uv run python -m scripts.roadmap_curate "${roadmap_args[@]}" \
    >> "$LOG_DIR/${TIMESTAMP}_roadmap.log" 2>&1
```

par :

```bash
  # 20m : premier run réel (2026-07-04) à 597s/600s — zéro marge sous 10m.
  # Pinned par tests/unit/test_dream_sh_roadmap.py.
  timeout 20m uv run python -m scripts.roadmap_curate "${roadmap_args[@]}" \
    >> "$LOG_DIR/${TIMESTAMP}_roadmap.log" 2>&1
```

- [ ] **Step 4: Vérifier le vert (pins dream.sh complets)**

Run: `env -u VIRTUAL_ENV uv run pytest tests/unit/test_dream_sh_roadmap.py tests/unit/test_dream_sh_phase_timeouts.py -v`
Expected: PASS partout.

- [ ] **Step 5: Gates + commit**

```bash
bash -n scripts/dream.sh
env -u VIRTUAL_ENV uv run ruff check tests/unit/test_dream_sh_roadmap.py
env -u VIRTUAL_ENV uv run ruff format --check tests/unit/test_dream_sh_roadmap.py
env -u VIRTUAL_ENV uv run pytest tests/unit -q
git add scripts/dream.sh tests/unit/test_dream_sh_roadmap.py
git commit -m "fix(dream): budget roadmap 10m→20m — premier run réel à 597s/600s"
```

---

## Gate final (avant merge)

```bash
env -u VIRTUAL_ENV uv run pytest tests/unit -q
env -u VIRTUAL_ENV uv run ruff check src/ tests/ scripts/
env -u VIRTUAL_ENV uv run ruff format --check src/ tests/ scripts/
env -u VIRTUAL_ENV uv run mypy src/
bash -n scripts/dream.sh
```

Puis review finale whole-branch (`git diff main..HEAD`) sur le modèle le plus
capable — les reviews par-task ne voient pas les interactions inter-étages
(learning SDD 2026-07-04).
