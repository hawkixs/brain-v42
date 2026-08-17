---
title: "Alembic fail-closed — explicit migration target and production opt-in"
status: completed
summary: "Supprimer tout fallback implicite vers la base live : POSTGRES_URL devient obligatoire pour Alembic, le DSN disparaît d'alembic.ini, et la base brain exige un opt-in de production explicite."
tags:
  - alembic
  - prod-safety
  - fail-closed
  - pattern-auto
  - sol-ultra
---

# Alembic fail-closed — explicit migration target and production opt-in

> Source : workstream SA1 de
> `docs/plans/2026-07-11-sol-ultra-audit-roadmap-plan.md`.
> Branche : `codex/startup-fail-closed-schema-gate`.
> Pattern : pattern-auto, plan à valider avant toute modification de code.

## Goal

Empêcher Alembic de sélectionner implicitement la base live `brain`. Une migration doit
recevoir `POSTGRES_URL` dans l'environnement du processus. Si la variable manque, si son URL
est invalide ou si elle cible `brain` sans confirmation explicite, Alembic doit s'arrêter
avant toute tentative de connexion.

Le correctif du 30 juin reste acquis : l'override `POSTGRES_URL` et le guard des tests
d'intégration fonctionnent. Ce chantier ferme uniquement le troisième niveau encore ouvert,
le fallback settings/`.env` puis `alembic.ini`.

## Architecture

`alembic/env.py` devient l'unique frontière de validation du target de migration :

1. lire seulement `POSTGRES_URL` depuis l'environnement du processus ;
2. parser l'URL avec `sqlalchemy.engine.make_url` ;
3. refuser toute query string, car le dialecte asyncpg peut l'utiliser pour écraser le host,
   le port ou la base après validation du chemin ;
4. exiger le driver `postgresql+asyncpg`, un host, un port TCP dans `1..65535`, un username,
   un password et un nom de base non vides ;
5. si le nom vaut exactement `brain`, exiger
   `BRAIN_ALEMBIC_ALLOW_PROD=1|true|yes` ;
6. retourner le DSN sans jamais le journaliser ;
7. injecter ce DSN dans la config Alembic avant le mode online ou offline.

`alembic.ini` conserve la clé `sqlalchemy.url`, requise par le contrat Alembic, mais sa
valeur reste vide. Il ne contient plus ni host, ni user, ni password, ni nom de base.

## Non-goals

- Ne pas créer ou modifier un rôle PostgreSQL live : cela exige une opération DB séparée et
  une décision de déploiement.
- Ne pas ajouter un schema-version gate au démarrage du serveur MCP ; c'est un chantier
  distinct si un besoin runtime est démontré.
- Ne pas exécuter de migration sur la base live pendant ce chantier.
- Ne pas modifier `AGENTS.md`, `CLAUDE.md` ou `uv.lock`, déjà modifiés avant cette session.
- Ne pas changer les migrations 001–031.

## Blast radius

GitNexus, avant plan : risque **LOW**, un caller direct (`alembic/env.py`), aucun process et
aucun module applicatif affecté. Le blast radius opérationnel reste élevé par nature : tout
appel Alembic sans env explicite cessera de fonctionner, volontairement.

## File structure

| Fichier | Changement |
|---|---|
| `alembic/env.py` | Résolution fail-closed, parsing et opt-in prod |
| `alembic.ini` | Retrait du DSN live, valeur `sqlalchemy.url` vide |
| `tests/unit/test_alembic_url_resolution.py` | Tests TDD du resolver et de la redaction |
| `tests/unit/test_alembic_cli_fail_closed.py` | Contrats subprocess hermétiques, wiring réel Alembic |
| `tests/unit/test_alembic_env.py` | Contrat structurel : aucun DSN dans l'ini |
| `README.md` | Commande opérateur avec opt-in prod explicite |

## Worktree preservation gate

Avant la phase RED, enregistrer les SHA-256 et le hash du diff de `AGENTS.md`, `CLAUDE.md`
et `uv.lock`. Refaire les mêmes calculs avant chaque commit. Toutes les commandes Python
utilisent `uv run --frozen` afin de ne jamais synchroniser ou réécrire le lockfile.

Après chaque task d'implémentation, pattern-auto impose deux checkpoints avant de continuer :
review de conformité au plan, puis review qualité/tests du diff de la task.

## Task 1 — Verrouiller le contrat du resolver en RED

**Files:**

- Modify: `tests/unit/test_alembic_url_resolution.py`

1. Adapter `_get_resolver()` au contrat sans argument.
2. Conserver le test de priorité de `POSTGRES_URL`, renommé en test d'acceptation explicite.
3. Remplacer les tests de fallback par les cas suivants :
   - env absente → `RuntimeError` mentionnant `POSTGRES_URL`, sans import de settings ;
   - URL malformée contenant un secret sentinelle → erreur générique levée `from None`,
     sentinelle absente de la cause et du traceback ;
   - query string présente, notamment `?database=brain` sur `/brain_test` → refus sans fuite ;
   - driver autre que `postgresql+asyncpg` → refus ;
   - base absente → refus ;
   - host, port, username ou password absent, et port hors plage → refus avant connexion ;
   - base `brain_test` → acceptée sans opt-in ;
   - base `brain` → refusée sans opt-in ;
   - base `brain` → acceptée avec `BRAIN_ALEMBIC_ALLOW_PROD=1`, `true` et `yes` ;
   - valeur d'opt-in inconnue → refus.
4. Exécuter le module ciblé et constater les échecs avant implémentation :

```bash
env -u VIRTUAL_ENV uv run --frozen pytest tests/unit/test_alembic_url_resolution.py -q
```

Expected RED : le resolver actuel accepte encore un argument, lit settings et retourne le
fallback ini.

## Task 2 — Implémenter la frontière fail-closed

**Files:**

- Modify: `alembic/env.py`
- Test: `tests/unit/test_alembic_url_resolution.py`

1. Importer `make_url` depuis `sqlalchemy.engine`.
2. Remplacer `_resolve_sqlalchemy_url(default: str)` par un resolver sans fallback.
3. Ne jamais interpoler le DSN fautif dans une exception ou un log.
   Toute conversion d'erreur utilise `raise RuntimeError(...) from None`.
4. Refuser toute query string avant de vérifier `drivername`, `database`, host, port
   `1..65535`, username, password et l'opt-in de la base `brain`. Le test documente que le
   dialecte asyncpg laisse `?database=brain` écraser le chemin `/brain_test` dans ses
   arguments de connexion effectifs. Des tests supplémentaires prouvent qu'aucun default
   asyncpg ne peut fournir implicitement l'endpoint ou l'identité.
5. Garder les constantes/allowlists utilisées par le resolver dans la fonction afin que le
   test AST ne masque pas une dépendance globale ; les tests CLI valident le wiring complet.
6. Supprimer `_ini_url`; appeler le resolver, puis injecter
   `resolved_url.replace("%", "%%")` dans `config.set_main_option`. Le resolver continue à
   retourner le DSN original.
7. Nettoyer les annotations/imports d'`alembic/env.py` pour que le fichier passe mypy sans
   `unused-ignore` ni redéfinition de `target_metadata`.
8. Exécuter les tests ciblés jusqu'au GREEN.
9. Exécuter aussi les tests de garde DB existants :

```bash
env -u VIRTUAL_ENV uv run --frozen pytest \
  tests/unit/test_alembic_url_resolution.py \
  tests/unit/test_integration_db_guard.py -q
```

## Task 3 — Supprimer le secret statique et documenter l'opération prod

**Files:**

- Modify: `alembic.ini`
- Modify: `tests/unit/test_alembic_env.py`
- Modify: `README.md`

1. Écrire d'abord un test qui refuse toute valeur non vide pour `sqlalchemy.url` dans
   `alembic.ini` et vérifie l'absence de `brain:brain` dans l'ini et le README.
2. Constater le RED sur l'ini actuel.
3. Remplacer le DSN par `sqlalchemy.url =` et expliquer que `alembic/env.py` injecte la
   valeur explicite.
4. Mettre à jour le Quick Start :

```bash
export POSTGRES_URL="postgresql+asyncpg://brain:REPLACE_WITH_PASSWORD@localhost:5433/brain"
BRAIN_ALEMBIC_ALLOW_PROD=1 alembic upgrade head
```

La documentation ne doit pas republier le vrai password, y compris dans la section
Configuration. Elle doit préciser que l'opt-in n'est requis que lorsque le nom de base vaut
exactement `brain` et doit rester ponctuel, jamais exporté durablement dans le shell.
5. Rejouer les deux modules unitaires Alembic.

## Task 4 — Prouver le wiring CLI et les 31 migrations

**Files:**

- Create: `tests/unit/test_alembic_cli_fail_closed.py`

1. Écrire des tests subprocess avec `sys.executable -m alembic`, environnement nettoyé,
   `cwd` fixé, timeout et stdout/stderr capturés. Utiliser une config temporaire dont
   `script_location` pointe vers le répertoire Alembic réel et un `.env` sentinelle dans le
   cwd temporaire. Prouver que, sans `POSTGRES_URL` processus, Alembic échoue sur le guard et
   n'utilise pas ce `.env`.
2. Tester une URL malformée dont le port contient un secret sentinelle. Vérifier le stderr
   subprocess complet et l'absence de la sentinelle.
3. Tester un password encodé contenant `%40` en mode offline. L'appel doit réussir et la
   valeur sensible doit être absente du stderr.
4. Automatiser les deux contrats offline suivants :

```bash
POSTGRES_URL="postgresql+asyncpg://brain:encoded%40secret@localhost:5433/brain_test" \
  env -u VIRTUAL_ENV uv run --frozen alembic upgrade head --sql
```

Expected : `brain_test` rend les 31 migrations sans connexion ; `brain` sans opt-in échoue
avant rendu SQL.

5. Ne pas exécuter le cas prod avec opt-in : son acceptance est couverte par le resolver
   unitaire et la documentation ; une vraie migration live est hors scope.
6. Lancer un PostgreSQL/pgvector éphémère nommé `brain_test`, sur un port loopback dédié et
   sans volume hôte. Appliquer réellement `alembic upgrade head`, vérifier `alembic current`
   et `alembic heads`, puis détruire uniquement ce conteneur temporaire. Ne réutiliser ni le
   conteneur `brain_v42_postgres`, ni le port 5433, ni un DSN issu de `.env`.
7. Enregistrer une décision Brain séparée sur le rôle de migration : recommandation
   `brain_migrator` dédié, permissions DDL nécessaires et coût du bootstrap ; application
   différée à une opération live explicitement autorisée.

## Task 5 — Gates et review de branche

1. Lancer les gates ciblés :

```bash
env -u VIRTUAL_ENV uv run --frozen pytest \
  tests/unit/test_alembic_url_resolution.py \
  tests/unit/test_alembic_cli_fail_closed.py \
  tests/unit/test_alembic_env.py \
  tests/unit/test_integration_db_guard.py -q
env -u VIRTUAL_ENV uv run --frozen ruff check alembic/env.py tests/unit/test_alembic_url_resolution.py tests/unit/test_alembic_cli_fail_closed.py tests/unit/test_alembic_env.py
env -u VIRTUAL_ENV uv run --frozen ruff format --check alembic/env.py tests/unit/test_alembic_url_resolution.py tests/unit/test_alembic_cli_fail_closed.py tests/unit/test_alembic_env.py
env -u VIRTUAL_ENV uv run --frozen mypy alembic/env.py src/
```

2. Lancer la suite unitaire complète.
3. Appliquer `gitnexus_detect_changes(scope="all")`.
4. Faire reviewer le diff complet par un juge final. Verdict attendu : `SHIP`.
5. Indexer ce plan dans Brain, épingler sa feature en `building`, puis la passer à
   `deployed` ou `done` uniquement après preuve et merge.

## Acceptance criteria

- Alembic sans `POSTGRES_URL` échoue avant connexion, même si `.env` existe.
- Un `POSTGRES_URL` invalide n'apparaît jamais dans l'erreur ou sa chaîne de causes.
- Toute query string est refusée avant connexion ; elle ne peut pas écraser la cible validée.
- Host, port TCP, username et password sont explicites ; aucun default asyncpg ne complète
  la cible ou l'identité.
- Un DSN valide contenant `%` traverse ConfigParser et le mode offline sans fuite.
- Aucun DSN réel ou credential n'est stocké dans `alembic.ini` ou ajouté au README.
- `brain_test` fonctionne sans opt-in ; `brain` exige un opt-in explicite.
- Les 31 migrations s'appliquent réellement sur un PostgreSQL/pgvector éphémère et atteignent
  le head attendu.
- Les migrations existantes et leur ordre restent inchangés.
- Les tests unitaires et gates statiques sont verts.
- Les modifications préexistantes du worktree restent non stagées et inchangées.

## Execution evidence — 11 juillet 2026

Le pattern-auto a convergé après deux passes de jugement du plan, puis chaque slice a reçu
une review spec et une review qualité indépendantes. Le premier review du resolver a trouvé
une fuite encore introspectable via `RuntimeError.__context__`; le correctif a déplacé la
levée générique hors du bloc `except`, puis les deux reviewers ont rendu `SHIP`.

Preuves fonctionnelles et statiques :

- resolver et garde DB : 34 tests passés ;
- contrats Alembic ciblés : 66 tests passés ;
- suite unitaire complète : 2 901 tests passés, 39 ignorés, 8 warnings préexistants ;
- Ruff check et format : passés ;
- mypy : aucun problème dans 112 fichiers source ;
- `AGENTS.md`, `CLAUDE.md` et `uv.lock` conservent leurs hashes initiaux et restent hors du
  staging.

Les quatre tests CLI prouvent le wiring final. Leur premier échec (62 occurrences au lieu de
31) était un bug de comptage du test entre stdout et stderr, pas un nouveau défaut produit ;
la preuve RED produit vient du resolver historique et du DSN statique d'`alembic.ini`.

La revue finale a ensuite trouvé un second P1 : avec `/brain_test?database=brain`, SQLAlchemy
exposait encore `brain_test` dans l'URL parsée mais le dialecte asyncpg transmettait
effectivement `database=brain`. Quatre tests RED (`database`, `host`, `port`, `ssl`) ont
prouvé le bypass. Alembic refuse désormais toute query string ; 60/60 contrats ciblés sont
verts après ce patch. La même review a ensuite montré qu'un DSN sans host, port ou identité
laissait asyncpg choisir des defaults implicites. Six tests RED ont fermé ce dernier chemin :
endpoint et identité sont maintenant complets, et 66/66 contrats ciblés sont verts.

Le rapport d'exécution de cette session — pas un transcript brut conservé — indique que le
drill réel a utilisé l'image runtime pgvector pinée, un conteneur unique en tmpfs, aucun
bind/volume et un port loopback aléatoire. Deux premières tentatives se sont arrêtées avant
migration sur des erreurs du harnais, avec cleanup exact-CID vérifié. La troisième a appliqué
`001 -> 031` sur `brain_test` avec `BRAIN_ALEMBIC_ALLOW_PROD` absent. Résultats :

- `alembic current` et `alembic heads` : `031 (head)` ;
- `alembic_version` : `031` ; pgvector : `0.8.4` ;
- 23 tables publiques, vue `codex_brain_entity_v1` présente, rôle `codex_ro` présent ;
- aucun index invalide ou non prêt ;
- conteneur jetable détruit et ID de `brain_v42_postgres` identique avant/après.

Limite de preuve : le transcript et le CID final du conteneur jetable n'ont pas été
conservés. Une vérification read-only post-session confirme le conteneur principal `running`
sur l'image pinée et l'absence de tout conteneur portant le label du drill. Un runbook avec
capture d'artefact durable relève du futur durcissement DR/OPS.

La décision Brain `a665e495-3a92-4a46-852d-5c90177c6e06` retient un futur rôle
`brain_migrator` sans privilèges cluster, sépare le bootstrap privilégié des migrations
courantes et diffère toute mutation live.
