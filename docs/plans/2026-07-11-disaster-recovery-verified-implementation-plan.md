---
title: "Disaster recovery vérifiable — PostgreSQL + Neo4j + off-site"
status: active
summary: "Transformer red-backup en preuve de reprise fail-closed : manifeste canonique, restore PostgreSQL réellement isolé et attesté, puis Neo4j/off-host/scheduling derrière des gates d'autorité explicites."
tags:
  - disaster-recovery
  - red-backup
  - postgresql
  - neo4j
  - offsite
  - pattern-auto
  - sol-ultra
---

# Disaster recovery vérifiable — PostgreSQL + Neo4j + off-site

> **Amendement de sûreté — 24 juillet 2026.** La décision Brain
> `3d3d72e4-acb7-49fe-aabb-1618e648e627` adopte l'option A « PostgreSQL canonique +
> rebuild-on-doubt ». Pour le graph ledger, une restauration Neo4j exacte/corrélée n'est plus
> un gate. La preuve head 035 est historique depuis le passage de la production à 037. Le run
> DR-v5 `20260724_150315` a renouvelé le gate PostgreSQL au head 037 avec 24/24 contrôles.
> Il reste à rejouer les rôles, propriétaires et ACL, puis à reconstruire une projection Neo4j
> dédiée et vide avec le protocole graph introduit en 035. Ne jamais downgrader pour fermer un
> gate. Les autres preuves DR de ce plan restent inchangées.

> Source : workstream DR1 de
> `docs/plans/2026-07-11-sol-ultra-audit-roadmap-plan.md`.
> Branches coordonnées : `codex/disaster-recovery-verified` dans `brain_v42` et
> `red-backup`. Pattern : pattern-auto ; aucun changement externe avant convergence des
> juges exigences, architecture et qualité.
> Checkpoint de reprise actif :
> [preuve opérationnelle B3](2026-07-12-disaster-recovery-b3-operational-evidence.md). Le
> [handoff B2](2026-07-11-disaster-recovery-b2-session-handoff.md) reste historique.

## Goal

Passer d'une sauvegarde locale structurellement lisible à une reprise vérifiable. Le premier
incrément livré doit rendre impossible un vert `0/0`, restaurer le run Brain audité dans un
PostgreSQL/pgvector jetable sans toucher à un cluster existant et produire une preuve JSON
expurgée. La feature DR1 reste `building` tant que Neo4j et une copie chiffrée hors hôte ne
sont pas réellement restaurables.

## Threat model et état prouvé au 11 juillet 2026

Le modèle reste LAN-only et agents personnels. Les scénarios DR pertinents sont la perte du
NVMe ou de l'hôte, la corruption silencieuse, une suppression privilégiée, un ransomware
local, une archive partielle, un restore incomplet annoncé vert et la perte des relations
Neo4j non dérivables.

État sain :

- run audité explicitement sélectionné : `20260711_030001`; dump Brain de 44 951 937 octets,
  SHA-256 `7e0c34ddd4e863a07482d4a66d30864d5b5e04e63743610e2cd1b9305951daf2` ;
- 16 générations Brain quotidiennes, 16/16 SHA et gzip valides ;
- dernier catalogue lisible : 207 entrées TOC, 23 `TABLE DATA`, 73 index, 45 contraintes et
  2 extensions ;
- 14 runs globaux consécutifs à 7/7 depuis le 28 juin ;
- baseline `red-backup` : 321 tests passés, 2 ignorés, 3 warnings existants ; dépôt `main`
  propre au commit `5328089` ;
- le dump ne fixe pas la version de l'extension (`CREATE EXTENSION ...` sans `VERSION`) et les
  images flottante/live exposent désormais 0.8.4. L'image officielle exacte 0.8.2/PG16 est
  donc pinée à l'index digest `00ba258a…`, téléchargée et à valider avant le vrai drill.

Défauts confirmés :

- `manifest.py` écrit un manifeste DB plat, tandis que `verify.py` attend `entries=[]` puis
  considère `0 == 0` comme un succès ; un dump volontairement corrompu a reproduit ce faux
  vert ;
- le CLI sans manifeste sort 0 ;
- `restore_test.py` peut annoncer un succès avec `pg_restore rc=1` et zéro table, dépend du
  `pg_restore` hôte absent, construit le flux gzip via shell et crée une base temporaire sur
  le cluster fourni ;
- sur le run réel `20260711_030001`, `scan_orphans()` classe 41 artefacts valides comme
  orphelins — les cinq dumps, leurs sidecars SHA et les snapshots — tandis que la rétention
  destructive est appelée automatiquement après chaque run vert ;
- PostgreSQL, Neo4j, backups et repos sont sur le même `/dev/nvme0n1p3` ; aucun `.gpg`, reçu
  distant ou artefact Neo4j n'existe ;
- dumps/répertoires sont en `0664/0775`, et des snapshots compose world-readable contiennent
  des clés lexicalement sensibles ;
- le cron 05:00 fonctionne mais n'a ni catch-up ni alerte prouvée ; le timer systemd n'est pas
  installé et son hardening masque la configuration SSH nécessaire à `red-writer-prod`.

## Architecture et ownership

`red-backup` possède l'orchestration technique : manifestes, sélection d'artefact,
conteneurs jetables, preuves, chiffrement, transfert, scheduling et cleanup.

`brain_v42` possède les invariants métier de reprise dans un contrat JSON versionné avec
`contract_id` et SHA, plus une attestation SQL fixe versionnée. `red-backup` en conserve des
copies vendored byte-identical ; chaque repo valide localement les documents et un gate
coordonné compare les SHA. Aucun test ne dépend d'un worktree `/tmp` ni d'un import Python
entre repos. Le DSL produit la preuve principale ; le script SQL exécuté par un second
processus `psql` produit une attestation indépendante au format JSON canonique.

Les checks emploient un DSL fermé, jamais du SQL libre dans YAML. Le contrat Brain fixe :

- les 23 tables exactes : `access_log`, `adrs`, `alembic_version`, `consolidation_log`,
  `decisions`, `dream_promotions`, `dream_runs`, `feature_artifacts`, `features`,
  `gitlab_events`, `indexed_plan_chunks`, `indexed_plans`, `learnings`,
  `metrics_timeseries`, `process_metrics`, `project_contexts`,
  `roadmap_curation_proposals`, `runbooks`, `search_log`, `snippets`,
  `ticket_extraction_proposals`, `ticket_messages`, `tickets` ;
- head `031`, extension `vector` version `0.8.2` pour le run épinglé, 17 foreign keys,
  101 index et zéro contrainte non validée ;
- corpus agrégé (`decisions`, `learnings`, `snippets`, `runbooks`, `adrs`) non vide,
  `project_contexts`, `indexed_plans`, `indexed_plan_chunks` et `features` non vides ;
- dimensions 1536 pour tout embedding non-null dans `decisions`, `learnings`, `snippets`,
  `runbooks`, `adrs`, `features`, `indexed_plans`, `indexed_plan_chunks` et `gitlab_events` ;
- typmod structurel `vector(1536)` pour ces neuf colonnes, même si une table ne contient
  aucun embedding non-null ;
- absence d'orphelins `indexed_plan_chunks → indexed_plans` et
  `feature_artifacts → features`.

Le moteur de restore PostgreSQL suit ce flux :

1. sélectionner explicitement un run et une target ;
2. charger le manifeste via un adaptateur fail-closed, puis vérifier existence, taille,
   checksum et compression ;
3. exiger l'image locale immuable
   `pgvector/pgvector@sha256:00ba258a66dac104fd5171074a0084462a64a1369d8513f3d0a634e2f24d15bc`
   avec `--pull=never` ;
4. exécuter un preflight Docker/image/RAM puis `docker create`; capturer et valider le CID
   avant `docker start` ;
5. imposer `--network none`, aucun port/bind/volume, PGDATA tmpfs 1 GiB, petits tmpfs pour
   `/var/run/postgresql` et `/tmp`, options `nodev,nosuid,noexec` si compatibles, mémoire et
   memory-swap 2 GiB, 2 CPU, 256 PIDs et timeouts bornés ;
6. initialiser `POSTGRES_DB=restore`, `POSTGRES_USER=restore_admin` et
   `POSTGRES_HOST_AUTH_METHOD=trust`, acceptable uniquement dans ce sandbox sans réseau ;
7. attendre `pg_isready` sur socket Unix puis exécuter `psql -X ... -c 'SELECT 1'` ;
8. streamer le gzip vers `docker exec -i CID pg_restore --dbname=restore
   --username=restore_admin --exit-on-error --single-transaction --no-owner --no-acl`, sans
   shell et sans charger tout le dump en mémoire ;
9. exécuter le moteur DSL, puis l'attestation SQL fixe dans un second processus
   `psql -X -v ON_ERROR_STOP=1`; comparer leurs JSON canoniques et conserver les checks en
   mémoire ;
10. dans `finally`, supprimer exclusivement le CID par `docker rm -f -v`, vérifier CID,
    label et volumes capturés absents, puis calculer le verdict global ;
11. écrire une seule fois le rapport JSON atomique, y compris sur échec. Le rapport final est
    donc post-cleanup ; un cleanup incomplet le rend rouge.

Le moteur n'accepte ni host, ni port, ni DSN live, ni argument Docker libre depuis YAML. Il
ne monte jamais `/data/backups` dans le conteneur et n'appelle jamais `CREATE/DROP DATABASE`
sur un cluster existant. Une racine read-only est activée seulement si le smoke test de
l'image pinée le prouve compatible.

Les sélecteurs CLI ne sont jamais interprétés comme des chemins. `RUN` doit matcher
`^\d{8}_\d{6}$`, désigner un enfant direct non-symlink de `storage_dir` et être ouvert sous
cette racine. `TARGET` doit matcher `^[a-z][a-z0-9_-]{0,63}$` et être une clé exacte du profil
chargé. Les chemins de manifeste et de rapport `.drills/<run>/<target>` sont reconstruits
uniquement depuis ces valeurs validées, puis prouvés sous leurs racines respectives. Un
répertoire de run neuf est créé exclusivement et n'est jamais réutilisé silencieusement.

Chaque `TargetManifestV2` lie explicitement l'artefact à son `contract_id` et son SHA lorsqu'un
profil de reprise existe ; le `RunManifestV2` contient la map exacte des bindings (valeur
explicite `null` pour une target sans contrat). Un restore refuse un contrat référencé absent :
il ne substitue jamais la version courante. Le reçu est sérialisé en JSON canonique ;
`.complete`, écrit atomiquement en dernier, contient le SHA-256 de ces octets. `verify-run`
exige reçu valide, sept succès exacts, marker présent et SHA identique.

## Worktree et protection des états utilisateur

- `brain_v42` reste dans le workspace courant ; les changements préexistants `AGENTS.md`,
  `CLAUDE.md` et `uv.lock` conservent leurs hashes et restent hors staging.
- `red-backup` est propre sur `main`. Créer un worktree isolé dans `/tmp` avec une branche
  `codex/disaster-recovery-verified`; ne pas modifier directement son checkout principal.
- Indexer ce worktree avec GitNexus avant édition. Le dépôt n'est actuellement ni indexé ni
  membre de `red-triad`; si l'indexation échoue, consigner la limite et utiliser les imports,
  tests et recherches textuelles comme blast radius explicite.
- Avant chaque commit, lancer `gitnexus_detect_changes` dans les repos indexés et revérifier
  les deux worktrees.

## Non-goals du premier incrément autonome

- Ne pas arrêter, redémarrer ou dumper Neo4j live.
- Ne pas écrire sur un second hôte, NAS, VPS ou disque sans choix explicite de destination.
- Ne pas installer d'unit systemd, retirer le cron, modifier un webhook ou des credentials.
- Ne pas changer en masse les permissions historiques sous `/data/backups` sans dry-run et
  autorisation opérateur.
- Ne pas activer `cleanup --execute`, `prune --execute` ou les anciennes branches orchestrator.
- Ne pas annoncer DR1 `done` après le seul restore PostgreSQL.

## File structure — incrément 1

### `red-backup`

| Fichier | Changement prévu |
|---|---|
| `src/backup/manifest.py` | Modèles canonique V2, adaptateurs legacy et écriture atomique |
| `src/backup/verify.py` | Vérification fail-closed des artefacts DB et snapshots |
| `src/backup/cleanup.py` | Références d'artefacts communes ; aucune suppression implicite |
| `src/backup/inventory.py` / `status.py` / `history.py` / `retention.py` | Run V2 complet vs legacy/unmanaged ; `.drills` hors inventaire et rétention |
| `src/backup/pg_dump.py` | Publication atomique et permissions du dump local |
| `src/backup/docker_dump.py` | Production V2 et modes `0600/0700` |
| `src/backup/ssh_docker_dump.py` | Production V2 et contrôle structurel cohérent |
| `src/backup/config_snapshot.py` | Copie puis chmod explicite, jamais confiance au mode source |
| `src/backup/runner.py` | Manifeste des snapshots ; aucun câblage restore automatique encore |
| `src/backup/config.py` | Profil déclaratif de restore, image immuable et invariants bornés |
| `src/backup/recovery_contract.py` | Modèles stricts du DSL et SHA du contrat vendored |
| `src/backup/restore_sandbox.py` | Lifecycle Docker borné et streaming sans shell |
| `src/backup/restore_checks.py` | Checks DSL, appel SQL fixe et comparaison canonique |
| `src/backup/restore_report.py` | Attestation locale JSON atomique post-cleanup |
| `src/backup/restore_test.py` | Shim refusant les anciens paramètres live |
| `src/backup/__main__.py` | `verify` non-zéro sans artefact ; commande `restore-drill` explicite |
| `deploy/systemd/red-backup.service` | `UMask=0077` seulement, sans installation live |
| `config/backup.yaml` | Profil Brain épinglé ; pas de destination distante fictive |
| `config/recovery/brain-v42-v1.json` / `.sql` | Copies vendored byte-identical du contrat et de l'attestation fixe Brain |
| `tests/` | RED/GREEN manifeste, CLI, cleanup, permissions et restore orchestré |
| `CLAUDE.md` | Documentation honnête : restore manuel prouvé, DR complet encore ouvert |

### `brain_v42`

| Fichier | Changement prévu |
|---|---|
| `ops/recovery/brain-v42-v1.json` | Contrat métier canonique avec `contract_id` |
| `ops/recovery/brain-v42-v1.sql` | Seconde attestation fixe, indépendante du compilateur DSL |
| `tests/unit/test_recovery_contract.py` | Le head attendu du profil DR suit la head Alembic |
| ce plan / roadmap | Preuves de session, limites et statut réel |

## Task 0 — Isoler, indexer et fixer les baselines

1. Créer le worktree `red-backup` dans `/tmp` depuis `origin/main`, branche
   `codex/disaster-recovery-verified`.
2. Enregistrer SHA HEAD, status, hashes des fichiers éventuellement sales et liste des
   branches anciennes ; ne fusionner aucune branche orchestrator.
3. Lancer `npx gitnexus analyze` dans le worktree, puis vérifier la disponibilité du repo.
4. Rejouer les 321 tests avec `uv run --frozen` et Ruff `src/`.
5. Dans Brain, vérifier que les migrations 001–031 restent inchangées et que la head est
   unique.

## Matrice de traçabilité DR1

| Finding | Clos dans cet incrément | Preuve | Bloque `done` |
|---|---|---|---|
| `verify 0/0` | Oui | verify-target + tests corruption | Oui tant que rouge |
| absence de restore/RTO PG | Oui | attestation locale du run épinglé | Oui tant que rouge |
| même NVMe | Non | mount audit | Oui |
| Neo4j non exact | Non | absence d'artefact + relation audit | Oui |
| absence off-host/chiffrement | Non | aucun reçu distant | Oui |
| cron sans catch-up/alerte | Non | audit scheduler/alerting | Oui |
| permissions historiques | Non | inventaire/dry-run | Oui pour secrets configs |

## Task 1A — Modèles V2 et adaptateurs legacy

**RED :**

- les deux `target_type` déployés (`docker_pg`, `ssh_docker_pg`) sont parsés ;
- un manifeste DB legacy valide référence réellement le dump et son sidecar `.sha256` ;
- dump absent, taille divergente, SHA divergente, gzip tronqué, manifest vide ou format
  inconnu échouent ;
- manifeste hybride, champ inconnu, doublon, composant vide, chemin absolu, `..`, backslash,
  symlink ou fichier non régulier sont refusés.

**GREEN :**

1. Introduire un schéma V2 strict (`extra='forbid'`) avec liste d'artefacts root-relative
   non vide, SHA 64 hex, timestamps timezone-aware, enums et listes `default_factory`,
   taille/sha/kind/compression et métadonnées de target.
2. Ancrer tous les chemins au run. Legacy DB possède `dump_file` et `<target>.sha256`; le
   format `entries[]` reste une fixture code mais n'est pas présenté comme format déployé.
3. Borner taille du manifeste et nombre d'artefacts. Ouvrir les fichiers avec `O_NOFOLLOW`,
   valider par `fstat` et conserver le même descripteur pour checksum/gzip/restore.
4. Porter dans les modèles le binding de contrat exact et refuser toute substitution implicite
   d'un contrat absent.

Checkpoint pattern-auto : review spec puis qualité.

## Task 1B — Verify ciblé et reçu de run complet

**RED :** zéro artefact, absence de manifeste/reçu/target, run ou target path-like, symlink de
run, marker absent, marker malformé, hash divergent, succès partiel ou target `skipped` rendent
le CLI non-zéro.

1. `verify-target RUN TARGET` peut attester une archive legacy individuelle, avec des sélecteurs
   syntaxiquement bornés et une target exacte du profil.
2. `verify-run RUN` exige un enfant direct non-symlink, un `RunManifestV2` listant les sept
   targets attendues, leurs bindings de contrat, leurs
   statuts et l'égalité exacte `expected == observed`; aucune target requise ne peut être
   `skipped`.
3. Le reçu canonique est publié après les target manifests ; `.complete` contient son SHA-256
   et n'existe que pour un run totalement réussi. `verify-run` recalcule ce SHA. Un run legacy
   sans manifestes snapshot reste `completeness=unknown` et non-zéro.
4. Le CLI sans manifeste, reçu, target attendue ou artefact sort non-zéro.
5. Faire un sweep read-only des 92 manifestes DB historiques et une vérification complète
   des 16 générations Brain, sans réécriture ni seconde lecture inutile d'une archive.

Checkpoint pattern-auto : review spec puis qualité.

## Task 1C — Inventory, status et cleanup fail-closed

**RED :** le dump DB et son `.sha256` ne deviennent jamais orphelins ; les snapshots sans
manifeste restent `legacy_unmanaged` ; un manifeste invalide annule le plan avant mutation ;
`.drills` n'apparaît ni dans inventory, status, history, cleanup, ni retention.

1. Faire consommer le même resolver d'artefacts à `inventory`, `status`, `history`, `cleanup`
   et `retention`, tout en excluant explicitement `${storage_dir}/.drills`.
2. Les snapshots historiques sans manifeste restent `legacy_unmanaged`.
3. Résoudre et valider le plan entier avant toute mutation. Le chemin destructif reste
   désactivé dans cet incrément ; si l'exécuteur interne est conservé, toute erreur I/O après
   mutation est rapportée `partial=true` et rouge, jamais comme un rollback réussi.
4. Ajouter une régression prouvant que le dump DB et son sidecar ne deviennent jamais
   orphelins, même avec d'autres manifests invalides.
5. `cleanup(dry_run=False)` et `prune_backups(dry_run=False)` échouent avant scan/mutation ;
   la rétention appelée après un run ne produit qu'un plan `dry_run=True` jusqu'à une review
   destructive séparée.

Checkpoint pattern-auto : review spec puis qualité.

## Task 1D — Producteurs atomiques et nouvelles permissions

1. Dumps, snapshots et métadonnées sont écrits sous noms temporaires sur le même filesystem,
   fsyncés et renommés. Le contrôle TOC sans shell réussit avant publication du manifeste
   target ; le reçu ne référence donc jamais un dump non contrôlé. Fsync du répertoire.
2. Le runner crée exclusivement le répertoire de run, publie le reçu canonique après toutes
   les targets puis `.complete` contenant son SHA-256, atomiquement et en dernier sur succès.
3. Nouveaux répertoires `0700`; chaque dump, snapshot, manifeste, historique, SHA et rapport
   reçoit un `chmod(0600)` explicite après écriture/copie. `umask` seul ne suffit pas.
4. Produire V2 pour les nouveaux outputs sans réécrire les archives historiques.

Checkpoint pattern-auto : review spec puis qualité.

## Task 2A — Contrat vendored, profil et preflight

**RED :**

- contrat ou SQL vendored dont le SHA diffère du canonique Brain échoue ;
- champ DSL inconnu, SQL libre, prédicat non borné ou binding de contrat absent échoue ;
- image flottante, capacité insuffisante, lock indisponible ou target sans profil sont refusés ;
- un profil qui permet port publié, bind, volume, réseau ou argument Docker libre est invalide.

**GREEN :**

1. Créer le contrat canonique Brain et sa copie vendored ; comparer leurs SHA aux gates.
2. Créer aussi l'attestation SQL fixe canonique et vendored. Valider le DSL borné et chaque
   prédicat négativement ; aucun SQL libre.
3. Preflight : image locale/digest, Docker, capacité, limites et lock de concurrence.
4. L'ancien `restore-test --host/--port` sort non-zéro ; aucun chemin live ne subsiste.

Checkpoint pattern-auto : review spec puis qualité.

## Task 2B — Lifecycle Docker et streaming

**RED :** tout `pg_restore` non-zéro échoue quel que soit stderr ; aucun appel
`asyncpg.connect`, `CREATE/DROP DATABASE` vers un host fourni ou shell n'est possible ; create
réussi/start échoué, état ambigu, timeout, gzip tronqué, broken pipe, deux drills concurrents et
cleanup non-zéro échouent.

1. `docker create`, capture CID, inspect des limites/mounts/réseau/ports, puis `docker start`.
   Si create/start retourne un état ambigu, résoudre uniquement le nom aléatoire, valider son
   label puis capturer le CID ; ne jamais supprimer par nom ou label.
2. Utiliser les limites codées en dur et timeouts startup/restore/checks/global.
3. Streamer depuis le descripteur déjà validé vers `pg_restore --dbname=restore
   --single-transaction --exit-on-error` ; gérer broken pipe et timeout.
4. Étendre l'interdiction du shell au contrôle TOC actuel de `docker_dump.py`.
5. Vérifier après cleanup l'absence du CID et des volumes capturés ; une ambiguïté laisse le
   drill rouge avec identifiants bornés pour intervention, jamais un glob de suppression.

Checkpoint pattern-auto : review spec puis qualité.

## Task 2C — Invariants, attestation et redaction

**RED :** zéro table, head/extension/compteur/typmod `vector(1536)` ou invariant manquant,
contrainte non validée, résultat `skipped`, mismatch DSL/SQL fixe, cleanup incomplet ou sentinelle
secrète dans une sortie rendent le drill rouge. Un test fault-injection force un faux résultat
sur un seul des deux chemins et prouve le mismatch.

1. Exécuter les checks exacts du contrat, chacun avec `expected`, `observed`, `status`; aucun
   check requis ne peut être absent ou skipped.
2. Exécuter le SQL fixe dans un second processus `psql`, sans réutiliser le compilateur DSL,
   canoniser séparément son JSON puis comparer exactement les deux attestations.
3. Construire le résultat en mémoire, nettoyer, calculer le verdict, puis écrire atomiquement
   sous `${storage_dir}/.drills/<run>/<target>/<drill-id>.json`, après reconstruction et contrôle
   de confinement du chemin. Toute commande d'inventaire ou de rétention ignore cette racine.
4. Enregistrer hash du dump, image résolue, `contract_id`/SHA,
   `attestation_sql_sha256`, head, compteurs, durées par phase et cleanup. Pour le legacy, écrire
   `source_completeness_comparison=unavailable`.
5. Ne conserver aucun transcript brut. Diagnostics autorisés : phase, code retour, taille et
   hash de stderr. Une sentinelle secrète est absente du rapport, logs et CLI.

Checkpoint pattern-auto : review spec puis qualité.

## Task 2D — CLI et test Docker opt-in

1. Exposer `restore-drill RUN TARGET`; aucun sélecteur implicite `latest`. Valider `RUN` et
   `TARGET` avant accès filesystem, puis reconstruire les chemins sous leurs racines.
2. Garder la suite hermétique mockée et ajouter un test d'intégration Docker opt-in distinct.
3. Le vrai drill du run épinglé est obligatoire avant SHIP.

Checkpoint pattern-auto : review spec puis qualité.

## Task 3 — Prouver le run Brain épinglé

1. Avant le run, enregistrer pour PostgreSQL et Neo4j un état normalisé : ID, running/status,
   restart count et healthcheck réel ; ne pas comparer le JSON Docker complet.
2. Exécuter `verify-target` sur les cinq manifestes DB du run ; obtenir des compteurs non
   nuls. `verify-run` reste non-zéro/completeness unknown pour ce run legacy sans manifestes
   snapshot.
3. Lancer `restore-drill` avec l'image pgvector pinée déjà présente localement.
4. La seconde attestation indépendante a lieu avant cleanup dans le moteur ; comparer les
   deux résultats dans le rapport et mesurer le RTO.
5. Prouver cleanup exact-CID, zéro conteneur/volume labellisé restant et IDs live identiques.
6. Indexer le rapport synthétique dans Brain comme « attestation locale », pas preuve durable
   de perte d'hôte.

## Task 4 — Durcir les nouvelles écritures sans mutation historique

1. Appliquer `umask 077` au CLI et `UMask=0077` au template systemd.
2. Prouver modes `0700/0600` sur dump local, Docker, SSH, snapshot, manifeste, SHA, historique
   et rapport dans un output temporaire.
3. Ajouter une commande de permission en dry-run seulement si nécessaire ; aucune option
   `--execute` dans cet incrément sans review séparée.
4. Documenter que les snapshots compose sont secrets et ne doivent jamais partir en clair.
5. Corriger la promesse documentaire « restore automatique » jusqu'à son vrai câblage.

## Task 5 — Gates, reviews et commits coordonnés

1. Tests `red-backup` complets, Ruff `src/` et modules modifiés, mypy si ajouté au projet.
2. Tests ciblés Brain et suite unitaire affectée.
3. Vérifier qu'aucune migration Brain ni archive historique n'a été modifiée.
4. `gitnexus_detect_changes` dans chaque repo indexé.
5. Review locale multi-perspective, reflexion et juge final pattern-auto ; verdict `SHIP`.
6. Commits atomiques séparés par repo ; aucun push/merge automatique avec les worktrees
   utilisateur sales.
7. Mettre la feature Brain à `building`, pas `done`.
8. Enregistrer les SHA des commits, retirer proprement le worktree `/tmp` après commit et
   vérifier que les branches coordonnées restent récupérables. Le checkout red-backup principal
   reste inchangé : c'est le rollback code immédiat. Le cron DR-v1, encore inchangé au checkpoint
   B2, a depuis été retiré au profit du timer DR-v3 actif.

## Lots nécessitant une nouvelle autorité

### Graph ledger — restore PostgreSQL + rebuild Neo4j (option A)

La restauration Neo4j exacte décrite initialement est supersédée pour le graph ledger.
Le run DR-v5 `20260724_150315` restaure PostgreSQL 16 au head 037 dans une cible isolée et passe
24/24 contrôles avec une attestation SQL indépendante. Une fenêtre offline doit encore rejouer
les rôles, propriétaires et ACL, puis reconstruire entièrement une base Neo4j dédiée et vide avec
le protocole de recovery graph introduit en 035. Comparer ensuite comptes, relations par type,
contraintes et requêtes sémantiques. Un dump Neo4j ne peut fermer aucun de ces gates.

### Off-host chiffré

Après choix d'un autre domaine de panne : chiffrer avant transfert, utiliser un
`known_hosts` provisionné et `StrictHostKeyChecking=yes`, staging + rename atomique, checksum
distant, reçu signé/horodaté, rétention indépendante et clé de récupération escrow hors du
PC serveur. Un autre chemin du même NVMe ne compte pas.

### Scheduling, alerting et permissions historiques

Le timer DR-v3 est installé, persistant et actif; le cron DR-v1 a été retiré après les preuves
automatiques historiques. Les répertoires et reçus courants utilisent `0700/0600`. Restent à
activer DR-v5 dans une livraison distincte, prouver un cycle planifié sous cette autorité,
activer le watchdog quotidien et recevoir une alerte Discord provoquée. Toute correction de
permissions historiques suit encore un inventaire/dry-run et un rollback documenté.

## Acceptance criteria de l'incrément autonome

- `verify-target` retourne un compteur non nul pour chaque manifeste DB et ne peut plus rendre
  `0/0 OK`.
- `verify-run` ne peut être vert que si un reçu V2 prouve les sept targets complètes ; tout run
  legacy incomplet reste non attesté.
- Tout artefact absent, tronqué, corrompu, vide ou hors racine rend le CLI non-zéro.
- Le dump Brain du run `20260711_030001` restaure réellement dans un conteneur pgvector sans
  réseau, port, bind ou volume hôte.
- `pg_restore` non-zéro, zéro table, invariant manquant ou cleanup incomplet rendent le drill
  rouge.
- L'attestation locale JSON contient hash, image, contrat, head, checks, compteurs, durées et
  cleanup ; elle est écrite après cleanup, même sur échec.
- Les conteneurs live gardent leurs ID, état, restart count et santé réelle ; aucun
  conteneur/volume temporaire ne reste.
- Les nouvelles sorties sont `0700/0600` et aucune archive historique n'est réécrite.
- Tests/gates/reviews sont verts dans les deux repos.
- DR1 reste `building` avec Neo4j/off-host/scheduler explicitement ouverts.

## SLO provisoires à mesurer

- RPO nominal PostgreSQL : 24 h lorsque le job quotidien réussit ; aucun RPO maximal n'est
  garanti avant scheduling persistant et alerte freshness < 26 h.
- Freshness attendue : backup local et future copie off-host < 26 h.
- RTO restore PostgreSQL sur hôte Docker prêt : objectif 30 minutes.
- RTO après perte complète de l'hôte : objectif 2 heures, non prouvé dans cet incrément.
- Drill PG quotidien ou hebdomadaire à trancher après coût mesuré ; rebuild Neo4j périodique
  depuis ce PostgreSQL restauré une fois la fenêtre offline autorisée.

Un RPO inférieur à 24 h exige un chantier PITR séparé (pgBackRest/WAL-G) ; le dump quotidien
ne peut pas le promettre.

## Checkpoint opérationnel B3

Le [checkpoint B3](2026-07-12-disaster-recovery-b3-operational-evidence.md) authentifie deux
cycles automatiques systemd consécutifs : runs `20260712_010222` et `20260713_010009`, chacun
avec sept targets et 42 artefacts. Le blocker « deux cycles automatiques » est clos. La
preuve PostgreSQL courante est renouvelée par le run DR-v5 `20260724_150315` au head 037 avec
24/24 contrôles. Le replay des rôles, propriétaires et ACL, le rebuild Neo4j dédié, l'activation
planifiée DR-v5, la copie off-host chiffrée et l'alerte Discord restent ouverts; DR1 conserve le
statut `building`.
