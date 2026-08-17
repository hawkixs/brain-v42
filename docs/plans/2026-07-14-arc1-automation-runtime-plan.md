---
title: "ARC1 lot 1 — séparer metrics et automation"
status: completed
plan_kind: implementation
summary: "Extraire GitLabIngestor et FeatureDedupJob dans un runtime brain_v42.automation dormant, conserver un rollback legacy explicite et prouver l'indépendance des cycles de vie sans toucher BrainService ni la surface MCP."
tags:
  - arc1
  - architecture
  - automation
  - metrics
  - pattern-auto
  - reversible-cutover
---

# ARC1 lot 1 — séparer metrics et automation

## But et rattachement

Ce plan exécute le premier lot borné d'ARC1 dans la roadmap Sol Ultra. Il traite uniquement
le couplage de disponibilité entre le sidecar métriques, le webhook GitLab et la
déduplication périodique des features.

Le lot ne ferme pas ARC1 globalement. Le typage de `build_services()`, le regroupement des
quinze dépendances de `BrainService` et la sortie du SQL des handlers MCP restent différés.

Point de départ : `main` à `8c36fec912f5fc31c375f03096ca5b184cb28f89`.
Branche : `feat/arc1-automation-runtime`.
Base et cible de merge/push : `main` vers `origin/main`, sans force.

### Gate d'autorisation pattern-auto

La commande `ok go focus` autorise la préparation du focus, mais ne vaut pas à elle seule
autorisation explicite de merge direct et de push de `main`. Avant Tâche 1, l'utilisateur
doit confirmer `pattern-auto` ou une autonomie de bout en bout incluant merge et push.
Sans cette confirmation, le livrable s'arrête à la branche isolée et au plan non commité :
aucun commit de tâche, merge ou push n'est effectué.

**Gate levé le 2026-07-15 :** l'utilisateur a confirmé explicitement `pattern-auto`.

Après confirmation et avant le premier RED :

1. rattacher un premier artefact durable à la feature stable
   `ARC1 lot 1 — séparer metrics et automation` ;
2. vérifier que ClusterGuard a créé une feature dédiée, sans rattachement ambigu à ARC1
   global ;
3. appliquer `brain_feature_update(..., status="building")` et vérifier la roadmap ;
4. conserver ARC1 global ouvert quel que soit le statut final de ce lot.

## État initial prouvé

`python -m brain_v42.metrics` possède aujourd'hui trois responsabilités :

1. servir `GET /metrics` et `GET /api/cockpit` ;
2. servir conditionnellement `POST /gitlab/webhook` ;
3. lancer `FeatureDedupJob` toutes les six heures.

Le succès du bind `:9200` conditionne le démarrage de la déduplication et un même signal
arrête les trois responsabilités. Aucun package `brain_v42.automation` ni unité systemd
versionnée n'existe. L'unité `brain-metrics.service` est locale à l'hôte et ne constitue
pas une source reproductible dans le dépôt.

Le webhook externe est actuellement décommissionné. Le lot ne le recrée pas, ne change pas
le bind loopback par défaut et n'effectue aucun déploiement.

## Contrats à préserver

### Metrics et cockpit

- `GET /metrics` et son JSON restent inchangés.
- `GET /api/cockpit` et son JSON restent inchangés.
- Le healthcheck embedding et le healthcheck Neo4j restent la propriété du runtime metrics.
- Le cleanup `process_metrics` / `search_log` reste la propriété du runtime metrics.
- Aucun changement de constructeur public de `MetricsServer` n'est requis.

### Webhook GitLab

Le même handler doit servir le chemin legacy et le nouveau runtime :

- route absente sans ingestor ;
- secret vide : `401 Webhook authentication not configured` ;
- token absent ou incorrect : `401 Invalid token` ;
- `X-Gitlab-Event-UUID` absent : `400` ;
- projet inconnu : `200 {"status":"unknown_project","path":"..."}` ;
- succès : appel exact `process_event(payload, event_uuid, project_key)` et JSON inchangé ;
- aucune nouvelle capture d'exception ou réécriture du payload.

Le resolver continue à lire `project_contexts.gitlab_project_path`. La surface MCP qui
alimente cette colonne et celle qui lit la roadmap ne changent pas.

### Déduplication

- première passe après le délai, pas au démarrage ;
- intervalle par défaut de 21 600 secondes ;
- parcours de toutes les clés projet ;
- une transaction et un commit par fusion ;
- protection `consumed_ids` inchangée ;
- propagation de `CancelledError` et log de toute autre exception ;
- mêmes noms d'événements `dedup_loop.*` ;
- barrières de propriété après la découverte, après la fusion, autour du commit et avant
  l'avancement ou le log durable.

Les builders automation et metrics legacy injectent `lease.ensure_owned` dans
`FeatureDedupJob`. Le job ré-embed après le `SELECT ... FOR UPDATE` et avant tout DML,
vérifie la propriété hors du handler best-effort d'embedding, puis avant et après chaque
await SQL. `feature_dedup.merge_staged` désigne une transaction non commitée ; seul
`dedup_loop.merged` signale une progression post-commit gardée.

### Compatibilité et propriété unique

- `METRICS_LEGACY_AUTOMATION_ENABLED=true` par défaut : déployer le code seul ne change pas
  le propriétaire live.
- Une lease PostgreSQL advisory non bloquante, sans migration, protège le propriétaire
  effectif. Elle utilise une clé `bigint` signée et stable, et une `AsyncConnection` dédiée
  en `AUTOCOMMIT`. Acquisition, heartbeat et libération utilisent cette même session et le
  même `pg_backend_pid()`. Metrics continue sans automation si la lease est occupée ; le
  nouveau runtime échoue explicitement.
- Un watcher borné par intervalle et timeout vérifie la connexion et son PID sans rappeler
  `pg_try_advisory_lock`. Toute perte ou invalidation déclenche une seule fois
  `ownership_lost` : le scheduler s'annule et le webhook devient fail-closed. Aucun runtime
  ne tente de reconnecter ou de réacquérir avant son redémarrage. Le nouveau runtime arrête
  son serveur et sort non-zéro ; le chemin metrics legacy reste metrics-only.
- La libération annule puis attend le watcher, appelle `pg_advisory_unlock` sur la connexion
  d'origine et exige `true`. Une réponse `false`, une erreur ou une annulation invalide la
  connexion physique avant sa fermeture. Le cleanup est idempotent et protège cette
  libération de l'annulation externe.
- `brain-v42-automation.service` est générée et vérifiée, mais jamais activée ni démarrée par
  l'installateur.
- Le cutover documenté interdit le dual-run : le propriétaire legacy est arrêté avant le
  démarrage du nouveau runtime.
- Le rollback restaure le chemin legacy avant toute réactivation externe du webhook.

## Architecture retenue

### Bounded context `brain_v42.automation`

Le nouveau package contient :

- `webhook.py` : handler HTTP unique, indépendant du serveur qui l'héberge ;
- `server.py` : serveur aiohttp automation, limité à `GET /health` et
  `POST /gitlab/webhook` ;
- `ownership.py` : lease PostgreSQL advisory de propriétaire unique ;
- `dedup.py` : boucle périodique déplacée, avec barrières de propriété autour des frontières
  de mutation et du commit ;
- `runtime.py` : composition typée et propriétaire explicite du cycle de vie ;
- `__main__.py` : entrée `python -m brain_v42.automation` et gestion SIGINT/SIGTERM ;
- `__init__.py` : surface minimale du package.

Une dataclass de composition porte les types concrets nécessaires au bounded context :
`AsyncEngine`, session factory, lease, embedding, reranker, ingestor, job et serveur. Elle
remplace le faisceau local de variables et permet une fermeture déterministe. L'ordre de
shutdown est : annuler/attendre dedup, arrêter/drainer aiohttp, fermer embedding et
reranker, libérer la lease, puis disposer l'engine.

`AutomationServer` borne le drain aiohttp à 10 secondes avec
`AppRunner(shutdown_timeout=10.0)`, contrat disponible dès la borne minimale supportée
`aiohttp>=3.9`. L'unité conserve `TimeoutStopSec=30`, soit une marge nominale de 20
secondes pour fermer embedding, reranker, libérer la lease et disposer l'engine.

La lease utilise exclusivement son `AsyncConnection` dédié pour acquisition, heartbeat et
libération. Elle expose un état et un événement typés `ownership_lost`; aucun autre checkout
du pool ne peut servir à prétendre que le verrou est encore détenu. Des wrappers vérifient
cet état après l'authentification et immédiatement avant `process_event`. Le handler traduit
une perte de propriété authentifiée en `503 {"status":"ownership_lost"}` sans appeler le
métier. Une requête non authentifiée reste `401`.

Le verrou advisory n'est pas un fencing token. Il garantit un propriétaire tant que la
session reste saine et un fail-closed après détection bornée ; il ne peut pas interrompre une
mutation déjà entrée dans PostgreSQL au moment exact d'une coupure réseau.

### Façade legacy

`MetricsServer` conserve sa signature et sa route conditionnelle. Son implémentation délègue
le traitement à `GitLabWebhookEndpoint`, ce qui garantit un seul contrat HTTP sans dupliquer
la logique. `metrics.__main__` ne construit la composition métier et ne lance la boucle que
si `metrics_legacy_automation_enabled` vaut `true`.

Quand ce flag vaut `false`, le processus metrics construit seulement collector, embedding
de santé, graphe de santé, serveur metrics et cleanup. La route webhook vaut alors `404`.
Quand le flag vaut `true` mais que la lease appartient déjà au nouveau runtime, metrics
journalise le conflit et continue avec ces seules responsabilités d'infrastructure.
L'acquisition de la lease legacy est bornée à deux secondes : une base indisponible annule
la tentative puis démarre également metrics-only, sans webhook ni scheduler legacy.

Le lifecycle metrics devient pilotable par un `stop_event` dans
`brain_v42.metrics.runtime`, appelé par `metrics.__main__`. Ce seam reste le vrai chemin de
production : il possède serveur, cleanup, lease legacy, dedup et shutdown. Sur perte de lease
legacy, il annule dedup et rend le handler webhook fail-closed sans arrêter `/metrics` ni
`/api/cockpit`.

Le fallback historique reste caractérisé : un `ImportError` du composant webhook désactive
le webhook sans désactiver la déduplication. La composition sépare donc le job obligatoire du
webhook optionnel au lieu de fusionner leurs deux modes de panne.

### Nouveau runtime

Le runtime automation :

1. construit les dépendances métier typées ;
2. acquiert la lease ; un conflit produit une sortie de configuration non nulle ;
3. démarre le serveur automation sur `127.0.0.1:9201` par défaut ;
4. échoue si le bind échoue, afin que systemd voie un démarrage rouge ;
5. lance la boucle de déduplication seulement après un bind réussi ;
6. attend un signal explicite ;
7. exécute toujours le cleanup complet dans un `try/finally`, y compris après un bind rouge.

`GET /health` est un signal de liveness du processus. Il n'est pas ajouté à la surface MCP
et ne prétend valider ni DB, ni embedding, ni reranker, ni progression du scheduler.

Configuration exacte, chargée par `Settings` :

- `automation_host` / `AUTOMATION_HOST`, défaut `127.0.0.1`, validé strictement loopback ;
- `automation_port` / `AUTOMATION_PORT`, défaut `9201`, borné à `1..65535` ;
- `automation_dedup_interval_seconds` / `AUTOMATION_DEDUP_INTERVAL_SECONDS`, défaut `21600`,
  strictement positif ;
- `metrics_legacy_automation_enabled` / `METRICS_LEGACY_AUTOMATION_ENABLED`, défaut `true`.

### Unité dormante

`deploy/systemd/brain-v42-automation.service.tmpl` :

- lance `.venv/bin/python -m brain_v42.automation` ;
- charge le `.env` existant ;
- journalise sous `brain-v42-automation` ;
- possède limites de crash-loop, restart et timeout d'arrêt ;
- ne déclare aucune relation `Requires=`, `Wants=`, `PartOf=`, `BindsTo=` ou `Conflicts=`
  vers `brain-metrics` ;
- est générée et vérifiée par `install.sh`, mais jamais `enable --now`.

## Critères d'acceptation du lot

1. Les contrats `/metrics`, `/api/cockpit`, webhook legacy et MCP passent sans changement.
2. `python -m brain_v42.automation` possède une composition typée et un arrêt gracieux.
3. Le nouveau serveur n'expose ni `/metrics` ni `/api/cockpit`.
4. Metrics avec le flag legacy désactivé ne construit ni `GitLabIngestor` ni
   `FeatureDedupJob`, ne lance aucune boucle métier et répond `404` au webhook.
5. Le chemin legacy activé conserve webhook et déduplication sans dérive observable, sauf
   lorsque la lease occupée l'oblige à rester metrics-only.
6. Arrêter le runtime metrics laisse le runtime automation sain ; arrêter automation laisse
   `/metrics` sain, prouvé avec le vrai `AutomationRuntime`, de vrais cycles start/stop et
   des sockets TCP éphémères.
7. Un test de double démarrage prouve qu'une seule lease et un seul scheduler existent.
8. Une perte de connexion dédiée annule le scheduler, ferme/refuse le webhook et libère
   toutes les ressources dans `finally`; le nouveau runtime sort non-zéro.
9. Le template systemd ne lie pas les cycles de vie et l'installateur n'active ni ne démarre
   jamais l'unité. Une unité déjà active reste un état opérateur, pas une garantie de
   l'installateur.
10. Le runbook contient préflight, cutover sans chevauchement, abort immédiat, smoke,
   rollback et condition séparée pour une éventuelle remise en service du hook GitLab.
11. Aucun fichier `BrainService`, handler MCP, registre d'outils ou schéma DB n'est modifié.

## Non-objectifs

- Déployer ou activer l'unité sur un hôte.
- Recréer le webhook GitLab externe ou ouvrir un bind LAN.
- Modifier `BrainService`, `build_services()` ou une signature/tool MCP.
- Refondre les politiques métier de `GitLabIngestor` ou `FeatureDedupJob` au-delà des gardes
  de propriété et de l'ordonnancement embedding-before-DML nécessaires au fail-closed.
- Ajouter une migration ou une nouvelle table de lease distribuée.
- Fermer ARC1 globalement.

## Tâche 1 — Extraire le contrat webhook et créer le serveur automation

Pré-impact obligatoire sur l'index GitNexus aligné avec `HEAD` :

- `MetricsServer._handle_webhook` (résoudre par contexte fichier avant impact) ;
- `ProjectKeyResolver` si son alias est déplacé.

Rapporter risque, appels directs et flux avant édition ; avertir de nouveau si GitNexus
retourne HIGH ou CRITICAL. Exécuter `gitnexus_detect_changes` avant le commit.

Fichiers :

- nouveau `src/brain_v42/automation/__init__.py` ;
- nouveau `src/brain_v42/automation/webhook.py` ;
- nouveau `src/brain_v42/automation/server.py` ;
- modifier `src/brain_v42/metrics/server.py` sans changer sa signature ;
- nouveau `tests/unit/automation/test_webhook.py` ;
- nouveau `tests/unit/automation/test_server.py` ;
- conserver `tests/unit/test_metrics_webhook.py` comme test de façade legacy.

RED obligatoire : commencer par un test d'import/existence borné, puis écrire séparément les
assertions comportementales webhook, `/health` et absence des routes metrics. Une erreur de
collection, un `ModuleNotFoundError` non ciblé ou une fixture cassée ne compte jamais comme
preuve RED. Chaque microcycle enregistre commande, code de sortie non nul, nom de
l'assertion et extrait montrant le défaut comportemental attendu.

GREEN : implémentation minimale du handler partagé et délégation legacy.

Gates ciblés :

```bash
pytest tests/unit/automation/test_webhook.py tests/unit/automation/test_server.py \
  tests/unit/test_metrics_webhook.py tests/unit/test_metrics_server.py \
  tests/unit/test_cockpit_endpoint.py tests/unit/metrics/test_pseudo_tools_filter.py -q
pytest tests/integration/test_recent_patches.py \
  tests/integration/metrics/test_metrics_contract.py \
  tests/integration/test_cockpit_endpoint_e2e.py -q
ruff check src/brain_v42/automation tests/unit/automation src/brain_v42/metrics/server.py
mypy src/brain_v42/automation src/brain_v42/metrics/server.py
```

Commit attendu : `refactor(automation): extract shared GitLab webhook boundary`.

## Tâche 2 — Composer et isoler les cycles de vie

Pré-impact obligatoire sur l'index GitNexus aligné avec `HEAD` :

- `Settings` ;
- `metrics.__main__.main`, résolu avec `gitnexus_context(name="main",
  file_path="src/brain_v42/metrics/__main__.py")` avant l'impact ;
- `_dedup_loop` ;
- toute méthode existante réellement modifiée après le diff de Tâche 1.

Rapporter le blast radius et l'avertissement HIGH/CRITICAL avant édition, puis exécuter
`gitnexus_detect_changes` avant commit.

Fichiers :

- nouveau `src/brain_v42/automation/dedup.py` ;
- nouveau `src/brain_v42/automation/ownership.py` ;
- nouveau `src/brain_v42/automation/runtime.py` ;
- nouveau `src/brain_v42/automation/__main__.py` ;
- modifier `src/brain_v42/automation/webhook.py` pour traduire la perte de propriété ;
- modifier `src/brain_v42/config.py` avec defaults additifs ;
- nouveau `src/brain_v42/metrics/runtime.py` pour le vrai lifecycle pilotable ;
- modifier `src/brain_v42/metrics/__main__.py` pour déléguer à ce lifecycle ;
- nouveau `tests/unit/automation/test_dedup.py` ;
- nouveau `tests/unit/automation/test_runtime.py` ;
- étendre `tests/unit/automation/test_webhook.py` avec la priorité auth et le `503` ;
- nouveau `tests/integration/test_automation_runtime_independence.py` ;
- adapter les imports de `tests/unit/test_dedup_loop_observability.py` en conservant les
  assertions ;
- étendre `tests/unit/test_config.py` ou ajouter un fichier de config borné.

RED obligatoire, observé séparément pour :

- defaults et surcharge env ;
- composition typée ;
- démarrage seulement après bind ;
- arrêt et fermeture ordonnée ;
- échec de bind fatal ;
- cleanup exactement une fois sur bind rouge, y compris runner, clients, lease et engine ;
- flag legacy `false` sans construction/tâche métier ;
- double démarrage refusé par la lease ;
- perte/invalidation ou changement de PID de la connexion dédiée : scheduler et webhook
  fail-closed, sans reconnexion ni seconde acquisition ;
- libération explicite sur la connexion d'origine, ou invalidation physique si son résultat
  reste incertain ;
- libération de lease dans `finally` sur tous les exits metrics legacy ;
- fallback `ImportError` : webhook absent mais dedup legacy conservée ;
- vrais entrypoints/cycles start-stop indépendants sur ports éphémères.

Le déplacement de `_dedup_loop` garde un alias temporaire dans `metrics.__main__` si un test
ou appel interne existant l'importe encore. Cet alias est une façade de rollback, pas un
second scheduler.

Le test d'indépendance démarre le vrai `AutomationRuntime` et le vrai lifecycle
`brain_v42.metrics.runtime` piloté par `stop_event`, pas deux apps aiohttp isolées. Il utilise
deux engines à pool distincts, vérifie par SQL que `current_database()` vaut `brain_test`, et
refuse de s'exécuter contre une autre base. Il arrête metrics puis obtient `200` sur
automation, redémarre metrics, arrête automation puis obtient `200` sur `/metrics`. Il
vérifie aussi l'absence de tâche métier legacy-off, la libération de lease metrics dans
`finally` et la fermeture exactement une fois des clients, de la lease et de l'engine. Un
test séparé termine le backend propriétaire et observe le fail-closed avant tout nouveau
traitement métier.

Gates ciblés :

```bash
pytest tests/unit/automation tests/unit/test_metrics_webhook.py \
  tests/unit/test_metrics_server.py tests/unit/test_dedup_loop_observability.py \
  tests/unit/test_feature_dedup_job.py tests/unit/test_feature_dedup_safety.py \
  tests/unit/test_gitlab_ingestor.py tests/unit/test_config.py \
  tests/integration/test_automation_runtime_independence.py -q
ruff check src/brain_v42/automation src/brain_v42/metrics/__main__.py \
  src/brain_v42/metrics/runtime.py \
  src/brain_v42/config.py tests/unit/automation \
  tests/integration/test_automation_runtime_independence.py
mypy src/brain_v42/automation src/brain_v42/metrics/__main__.py \
  src/brain_v42/metrics/runtime.py src/brain_v42/config.py
```

Commit attendu : `feat(automation): add independently managed runtime`.

## Tâche 3 — Livrer l'unité dormante et le runbook réversible

Pré-impact GitNexus sur tout symbole shell existant modifié dans `install.sh`, puis
`gitnexus_detect_changes` avant commit. Si GitNexus n'indexe pas le symbole shell, consigner
explicitement l'absence de cible et protéger la modification par les tests d'installation.

Fichiers :

- nouveau `deploy/systemd/brain-v42-automation.service.tmpl` ;
- modifier `deploy/systemd/install.sh` pour generate/verify/uninstall sans enable/start ;
- nouveau `tests/unit/deploy/test_automation_unit.py` ;
- adapter `tests/integration/test_dream_systemd_install.sh` pour la génération dormante ;
- nouveau `deploy/systemd/README.md` avec topologie, préflight, cutover et rollback ;
- modifier `README.md` pour distinguer metrics et automation ;
- modifier `docs/ARCHITECTURE.md` pour la topologie et la perte volontaire des événements
  automation dans `cockpit.recent` ;
- mettre à jour ce plan avec les preuves réelles.

RED obligatoire : après un test d'existence borné, les assertions doivent refuser toute
dépendance de cycle de vie vers metrics et toute activation dans l'installateur. Un faux
`systemctl` journalise les appels : aucun `start`/`enable` automation pendant l'installation,
mais `stop`/`disable` et suppression obligatoires à l'uninstall.

Cutover documenté, sans exécution live :

1. capturer l'unité host-local `brain-metrics`, les relations systemd, l'état des deux
   services, la valeur effective des environnements et la disponibilité de `:9201` ;
2. exécuter `daemon-reload` après génération et préparer une source d'environnement tardive
   dédiée, `EnvironmentFile=%h/.config/brain-v42/automation-owner.env`, contenant
   `METRICS_LEGACY_AUTOMATION_ENABLED=false` et protégée en mode `0600` ;
3. vérifier que cette source est la dernière de `EnvironmentFiles`, puis filtrer uniquement
   le flag dans `/proc/$MainPID/environ` après démarrage ;
4. arrêter metrics afin de libérer la lease du propriétaire legacy ;
5. démarrer automation et vérifier `GET :9201/health` ainsi que la lease unique ;
6. **abort sur échec** : arrêter automation, réactiver legacy, redémarrer metrics, vérifier
   `/metrics=200`, puis interrompre le cutover ;
7. redémarrer metrics et vérifier `/metrics=200`, webhook legacy `=404` et absence de second
   scheduler ;
8. seulement sur décision séparée, repointer un hook GitLab vers `:9201` ;
9. après soak explicite seulement, autoriser un `enable` manuel.

Rollback documenté, sans chevauchement :

1. désactiver le hook externe sans le repointer ;
2. arrêter/désactiver automation et vérifier la libération de lease ;
3. réactiver le flag legacy et vérifier l'environnement effectif ;
4. redémarrer metrics ;
5. vérifier `/metrics=200`, le contrat webhook legacy et l'unique scheduler ;
6. seulement après ce vert, repointer/réactiver le hook si une décision séparée le demande ;
7. ne supprimer ni template ni drop-in avant la fin du soak.

Gates ciblés :

```bash
pytest tests/unit/deploy/test_automation_unit.py -q
REQUIRE_SYSTEMD_ANALYZE=1 bash tests/integration/test_dream_systemd_install.sh
# Sur un hôte dont le manager user est disponible, rendre aussi la sonde transitoire obligatoire :
REQUIRE_SYSTEMD_ANALYZE=1 REQUIRE_USER_SYSTEMD=1 \
  bash tests/integration/test_dream_systemd_install.sh
# systemd-analyze vérifie le .service généré sous XDG_CONFIG_HOME temporaire,
# après substitution de __REPO_ROOT__; le template brut n'est jamais vérifié.
```

Commit attendu : `chore(automation): ship dormant systemd runtime`.

Preuves de livraison de la Tâche 3 :

- Les validations précommit utilisent uniquement des fixtures temporaires et des fakes
  pour leurs simulations de l'installateur et des blocs documentés. La gate d'intégration
  ajoute un vrai `systemd-analyze --user verify` et, si le manager user est disponible, une
  unité transitoire réelle qui prouve la précédence base `true` puis source tardive `false`.
- Le runbook décrit des commandes opérateur hôte sur le vrai répertoire user systemd; il ne
  prétend pas utiliser les fixtures ou les fakes des tests.
- Les blocs `Preflight`, `Cutover`, `Abort immédiat`, `Smoke tests` et `Rollback` sont
  extraits du Markdown et exécutés sous `set -euo pipefail`. Les tests injectent les échecs
  lease et HTTP pour vérifier qu'aucune mutation ultérieure n'est effectuée.
- La sonde de lease filtre la base courante, le mode `ExclusiveLock` et la représentation
  exacte de la clé advisory; elle exige `owners=<attendu> waiters=0`.
- GitNexus n'indexe ni `warn_wiped_env` ni `deploy/systemd/install.sh`; l'outil a retourné
  `Target not found` pour les deux cibles. La portée shell est donc protégée par les tests
  d'installation et la gate d'intégration.
- Preuves exécutées le 2026-07-15 : `35 passed` pour le contrat Task 3, `63 passed` pour
  `tests/unit/deploy`, puis `owners=0 waiters=0` contre `brain_test` avec les casts
  `oid::bigint`. La gate finale
  `REQUIRE_SYSTEMD_ANALYZE=1 REQUIRE_USER_SYSTEMD=1` a passé avec un manager user réel.
  Elle a vérifié l'unité générée et la précédence effective de la valeur de base `true`
  par la source tardive `false`.

## Gates finaux coordinateur

Après intégration des trois tâches et des durcissements de clôture :

```bash
pytest tests/unit -q
pytest tests/integration -q
REQUIRE_SYSTEMD_ANALYZE=1 bash tests/integration/test_dream_systemd_install.sh
ruff check src/ tests/
ruff format --check src/ tests/
mypy src/
```

Puis :

- `git diff --check` ;
- scan secrets/debug/unexpected files ;
- `gitnexus_detect_changes(scope="compare", base_ref="main")` avant chaque commit et avant
  merge ;
- review indépendante du diff complet ;
- post-merge : gates ciblés runtime + unit systemd + smoke des surfaces MCP ;
- push normal de `main` seulement si les refs local/upstream et les gates concordent.

## Risques et parades

| Risque | Parade |
|---|---|
| Dérive du webhook entre deux runtimes | Handler unique + tests paramétrés sur les deux hôtes |
| Deux propriétaires simultanés | Lease advisory sur connexion dédiée + test de conflit + séquence old-off/new-on |
| Perte silencieuse de lease | Heartbeat sur la même connexion + événement ownership_lost + fail-closed testé |
| Déploiement du code coupe l'automation | Flag legacy `true` par défaut |
| PostgreSQL indisponible retarde le bind metrics | Acquisition de lease legacy bornée à 2 s + fallback metrics-only testé |
| Arrêt metrics ferme automation | Aucun lien systemd et cycles de vie testés séparément |
| Arrêt automation coupe metrics | Serveur et ressources distincts, test d'intégration |
| Bind automation rouge mais scheduler actif | Bind avant tâche + `try/finally` + test cleanup exact |
| Drain HTTP dépasse le budget systemd | AppRunner borné à 10 s dans une unité bornée à 30 s + webhook en vol et déconnexion client testés |
| Réexposition accidentelle du webhook | Loopback par défaut, aucune activation ni hook dans ce lot |
| Perte des logs automation dans cockpit | Acceptée et documentée : `recent` est in-process ; journal systemd devient la preuve |
| Reprise partielle après perte post-commit | Risque Medium accepté : les transactions ClusterGuard, événement et artefact restent indépendantes. Une perte détectée après un commit peut rejouer un merge ou laisser `feature_artifacts` absent ; `gitlab_events.feature_id` conserve le lien. L'atomicité ou une réconciliation durable relève d'un lot ultérieur. |
| Commit dedup déjà lancé lors de la perte | Risque Medium accepté : la lease advisory ne peut ni annuler ni restaurer un `commit()` déjà entré dans PostgreSQL. La garde post-commit arrête le passage et les logs suivants. |
| Verrous feature pendant le ré-embedding | Risque Medium accepté : les deux lignes feature restent verrouillées entre le `FOR UPDATE` et la fin de l'await d'embedding. L'annulation borne le cas normal, mais un embedding bloqué peut prolonger ces verrous. |
| Réinstallation d'une unité avec overrides host-local | Le scanner reconstruit et compte les directives logiques `Environment=` sans afficher leurs valeurs ; toute erreur ou sortie non canonique arrête avant écrasement. Les autres directives manuelles restent hors scope et doivent être migrées en drop-ins avant même un `--dry-run`. |
| Casse de `MetricsServer` CRITICAL | Signature stable, délégation minimale, suite metrics/webhook complète |
| Dérive MCP | Aucun fichier MCP/BrainService modifié + suites MCP ciblées |

## Condition de livraison Brain

Avant Tâche 1, la feature stable `ARC1 lot 1 — séparer metrics et automation` doit exister
et être `building`. À la livraison, la mettre à jour avec le statut réel. Le statut `done`
du lot ne doit pas faire passer ARC1 global à `done`. Capitaliser séparément la décision de
lease propriétaire unique et un runbook de cutover ; ne pas fermer la session Brain sans
commande explicite de l'utilisateur.

## Preuves d'exécution — 15 juillet 2026

Les vingt-huit commits de plan, tests, implémentation et preuve précédant ce bilan bornent le
lot :

1. `ffd9c39` — plan de séparation metrics/automation ;
2. `bd97f76` — indexation du plan ;
3. `0e53dd5` — frontière webhook GitLab partagée ;
4. `82d6cd8` — contrat de propriété du runtime ;
5. `2535b63` — runtime automation indépendant ;
6. `8fbd06a` — unité systemd dormante et runbook ;
7. `9ecf394` — durcissement des preflights systemd ;
8. `25e8172` — reproduction des mutations webhook après perte de lease ;
9. `9b0d60c` — garde des mutations webhook après perte de lease ;
10. `3e6b524` — premier bilan des preuves de livraison ;
11. `8f8f554` — reproduction des mutations dedup après perte de lease ;
12. `ce50f98` — attente des barrières supplémentaires dans le scheduler ;
13. `49c7962` — garde des mutations dedup après perte de lease ;
14. `1b9d0a3` — reproduction de la fuite des valeurs `Environment=` ;
15. `1501cb4` — redaction des valeurs dans l'avertissement ;
16. `f13bb76` — reproduction des espaces et erreurs de scan ;
17. `c62cf92` — scan fail-closed des directives physiques ;
18. `1a7ea9e` — reproduction de l'indentation et des continuations logiques ;
19. `5800769` — scanner AWK des directives logiques `Environment=` ;
20. `3c11a0b` — bilan final des barrières de mutation ;
21. `7fe44a5` — reproduction du preflight non sûr et des runtimes imbriqués ;
22. `2d4c3f2` — preflight sans fuite et topologie des trois runtimes frères ;
23. `cf7b8a3` — reproduction du shutdown HTTP non borné ;
24. `d6410c9` — attente cliente asynchrone et bornée ;
25. `bb78f70` — drain HTTP borné à 10 secondes ;
26. `619b647` — bilan des preuves de livraison ;
27. `344bb8b` — reproduction de la lease legacy bloquant le bind metrics ;
28. `6ea57f6` — acquisition legacy bornée et fallback metrics-only.

La Tâche 4 a commencé sur le code pré-correctif par `9 failed, 36 passed`. Les deux RED
PostgreSQL ont observé `mutations(feature,event,artifact)=(1,1,1)` et `persisted=1`.
L'audit indépendant a rendu `APPROVE_TDD`. Le GREEN a ensuite produit `45/45` tests ciblés
et `2/2` tests PostgreSQL, puis les revues indépendantes ont trouvé zéro Critical et zéro
High. Les smokes post-fast-forward ont passé.

La Tâche 5 a commencé par `10/10` échecs unitaires. Son RED PostgreSQL a observé un
snapshot modifié et des `UPDATE FEATURES` / `DELETE FROM FEATURES` après la perte. Le GREEN
a produit `10/10` unités et `1/1` intégration : backend propriétaire terminé, successeur
réellement acquis, snapshot inchangé, zéro DML post-perte et verrous récupérables avec
`FOR UPDATE NOWAIT`. L'audit indépendant a rendu `APPROVE_TDD` et la revue concurrence n'a
trouvé aucun Critical, High ou Medium.

La Tâche 6 a exécuté trois cycles RED/GREEN. Ils ont reproduit la fuite initiale, les erreurs
scanner ignorées, la sortie non numérique, l'indentation initiale et les continuations de
ligne. Le GREEN final passe les `40/40` contrats installateur, ne réémet ni valeurs ni sortie
scanner, et échoue avant écrasement sur code non nul ou compteur invalide. Les audits
indépendants ont rendu `APPROVE_TDD` ; la revue finale n'a trouvé aucun Critical, High ou
Medium.

La correction documentaire de clôture a observé `3/3` échecs RED : présence de
`systemctl --user cat`, émission réelle d'un secret sentinelle et diagramme imbriquant les
runtimes. Le GREEN conserve uniquement des propriétés non sensibles via `systemctl show`,
dont `FragmentPath` et `DropInPaths`, représente FastMCP, metrics et automation comme trois
boîtes sœurs, puis passe `42/42` contrats. L'audit indépendant a rendu `APPROVE_TDD` sans
Critical, High ou Medium.

La correction shutdown a observé `2/2` échecs RED sur le défaut aiohttp non borné et
l'absence d'override. Après un raffinement RED de l'attente cliente, le GREEN passe `2/2`
contrats puis `24/24` tests serveur/runtime. Un test de mutation qui accepte l'option sans
la transmettre à `AppRunner` expire bien sur le webhook bloqué. L'audit indépendant a
rendu `APPROVE_TDD` et la revue corrigée a trouvé zéro Critical, High ou Medium. Le seul
point Low est que le test observe la fin du client plutôt qu'un `finally` explicite du
handler ; `runner.cleanup()` et les tests d'ordre du runtime couvrent le comportement.

La revue finale du diff complet a ensuite trouvé que l'ouverture PostgreSQL de la lease
legacy pouvait différer le bind metrics jusqu'au timeout du driver. Le RED a bloqué
`lease.acquire()` et observé un `TimeoutError` externe avant tout bind. Le GREEN borne cette
tentative à deux secondes, vérifie son annulation puis démarre metrics-only ; il passe `1/1`
contrat, `10/10` tests du runtime metrics et `6/6` intégrations d'indépendance. L'audit a
rendu `APPROVE_TDD` et la revue corrigée `APPROVE`, sans Critical, High ou Medium. Le seul
point Low est le second log `legacy_lease_conflict` après le log précis de timeout.

Les gates finales ont produit :

- unités, avec sockets loopback autorisées pour les tests aiohttp :
  `3261 passed, 48 skipped, 8 warnings` ;
- intégrations avec `POSTGRES_URL` et `BRAIN_V42_TEST_DB_URL` pointant vers `brain_test` :
  `119 passed, 2 warnings` ;
- `ruff check` vert et `ruff format --check` : `380 files already formatted` ;
- `mypy` vert sur `129 source files` ;
- gate systemd réelle obligatoire
  `REQUIRE_SYSTEMD_ANALYZE=1 REQUIRE_USER_SYSTEMD=1` : PASS, manager user réel et
  précédence `true` vers `false` vérifiée ;
- GitNexus : risque CRITICAL attendu sur `36` fichiers, `992` symboles et `44` flux.

Aucun déploiement, redémarrage de service ou hook externe n'a été exécuté. Le statut
`completed` couvre l'implémentation et ses preuves sur la branche de feature. Le merge et
le push de `main`, puis l'actualisation de la feature, de la décision et du runbook Brain,
restent les conditions de livraison ; la session Brain reste ouverte.
