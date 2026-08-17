# Campagne de maintenance — 2026-08-03

Plan de maintenance stricte ouvert après le bilan des trois semaines du 13/07 au 03/08.
Destiné à survivre aux compactions de contexte : toute session peut le reprendre tel quel.

## Pourquoi ce plan

Trois semaines de travail intense ont produit beaucoup de volume et peu de livraison
observable. Ce plan cadre le rattrapage et pose la règle qui empêche la rechute.

## Constat de départ (vérifié le 2026-08-03)

### Le code est sain — ce n'est pas un plan de réparation

```
ruff check       : All checks passed
ruff format      : 555 files already formatted
mypy src/        : Success, no issues in 170 source files
cycles modules   : 0   (preflight ratchet du 30/07 opérationnel)
tests collectés  : 6 742
```

Aucune dette lint, aucune dette typage, graphe de modules acyclique. **Le refactor à
mener est un dégraissage, pas une correction.** Toute tâche formulée comme « corriger le
code » sur cette campagne part d'une prémisse fausse.

### Le volume est le problème

| Zone | Lignes |
|---|---|
| `tests/` | 136 527 |
| `src/` | 42 332 |
| `scripts/` | 24 362 |
| `services/` | 1 380 |
| `docs/` | 54 258 |

**Ratio test/code de production : 2,01:1** (136 527 contre 68 074). Pour un projet à forte
charge de tests de contrat et de sécurité, c'est défendable.

> **ERRATUM 2026-08-03.** Une version antérieure de ce plan annonçait 3,2:1. Ce chiffre
> divisait les tests par `src/` seul, en omettant `scripts/` et `services/` — qui sont du
> code de production, et qui sont testés. Le ratio réel est 2,01:1.
>
> Le même erratum invalide le « cas emblématique » alors cité :
> `tests/unit/test_container_image_pins.py` (11 351 lignes) couvre
> `scripts/check_container_image_pins.py`, qui fait **11 220 lignes, 543 fonctions et
> 27 classes**. Le ratio y est de **1,01:1** — proportionné. Ce fichier de test n'est pas
> de la sur-ingénierie.
>
> L'observation qui survit est déplacée vers la source : un gate de pin d'images est devenu
> un analyseur statique AST de 11 220 lignes (scan des Dockerfiles, compose, CI YAML, shell
> et appels SDK Python) documenté par **16 lignes de docstring au total**. La question
> n'est pas « trop de tests » mais « pourquoi cette surface, et qui peut encore la relire ».

Sur les trois semaines : ≈320 commits, 110 579 insertions, 13 763 lignes de markdown.

Répartition : 122 `fix`, 69 `docs`, 55 `feat`, 51 `test`. Sur les 55 `feat`, 21 touchent
la surface MCP et une dizaine seulement ajoutent une capacité visible à l'usage. Les 46
autres sont de l'infrastructure interne. **C'est ce constat-là, et non le ratio de tests,
qui porte le diagnostic de non-livraison.**

### Ce qui est cassé ou bloqué

- `brain-v42-dream.service` **failed** depuis le 2026-08-02 06:15 (EXTRACT timeout sur un
  backlog d'embeddings non comparables). Ticket `d104660d`, `in_progress`.
- Killswitch ROADMAP toujours **DRY** après 19 nuits propres → **335 propositions de
  curation** en attente depuis le 14/07.
- Prod à la migration **037**, repo à **039**.
- Roadmap : ~148 entrées dont **58 pseudo-features `research`** (learnings auto-promus).
- **24 tickets** à traiter, 11 à confirmer, 5 en attente externe. Les plus vieux à 9-11 jours.
- 4 items `building` sans clôture, dont **Sol Ultra** (dernière activité 24/07).
- **Zéro item passé à `done`** sur les trois semaines.

### Outillage dégradé

GitNexus : 21 repos indexés, **2 entrées nommées `brain-v42`** — la racine canonique
(24 commits de retard) et le worktree `vigilant-euclid-da3597` (**893 commits de retard**).
Le registry est pollué au-delà du Brain : `red-writer` a 9 entrées de worktrees,
`refondrre` en a 3 dont une sous `/tmp`.

Git : **11 worktrees** (994 MB dans `.claude/worktrees` seuls), **32 branches** locales
dont 14 mergées et 17 non mergées.

`CLAUDE.md` annonce « 15 163 symbols, 31 473 relationships » ; l'index réel est à
19 245 nœuds / 37 418 arêtes. La doc est périmée sur ce point.

## Décisions actées

### D1 — Migrations 038/039 : on déploie, on n'abandonne pas

Décision opérateur du 2026-08-03. Les 6 400 lignes en suspens entre repo et prod
représentent du travail à conserver. Conséquence : la Phase 2.2 **garde** les 3 820 lignes
de tests `plan_index_repair`, et le rollout suit `docs/PLAN_INDEX_REPAIR_RUNBOOK.md`
(restauration isolée, ordre 038→039, redémarrage final).

### D2 — Sécurité déprioritée, mais la bombe CI est désamorcée séparément

Décision opérateur du 2026-08-03 : le volet sécurité n'est pas prioritaire.

Cette décision est **soutenue par les faits** : sur les findings du pipeline 4288
(sha `d2e37925`), Bandit donne 16 Medium / **0 High** dont 15 B608 faux positifs et 1
seule vraie décision (B104, bind réseau) ; Gitleaks donne 8+2 candidats, **tous faux
positifs, 0 secret, 0 rotation nécessaire**. Les preuves sont déjà conservées hors
artefacts expirables depuis le 01/08 (`~/.local/state/brain-v42/security-evidence/4288`,
SHA-256 enregistrés). Le risque sécurité réel est faible.

**Mais il reste une conséquence mécanique indépendante du risque sécurité.**
`.gitlab-ci.yml:27` porte `SECURITY_BURN_IN_UNTIL: "2026-08-22"`, et
`test_non_blocking_security_jobs_expire_at_the_burn_in_deadline` asserte
`not (today > deadline and non_blocking)`. Les 3 jobs sécurité sont encore `allow_failure`.

→ **Le 2026-08-23, la suite unitaire passe au rouge dans le job bloquant `test:unit`**,
pour tout travail, y compris sans rapport avec la sécurité.

Le commentaire du CI prévoit lui-même les deux issues : *« Flip them to blocking, or move
this date deliberately. »* La sortie compatible avec la dépriorisation est donc de
**déplacer la date par décision loggée** — quelques minutes, pas une campagne. C'est la
tâche 3.1 ci-dessous. Traiter les 45 avis pip-audit reste hors scope de cette campagne.

## Protocole d'orchestration

Établi le 2026-08-03.

**Attribution vérifiée du volume.** Sur les 320 commits de la période, 5 seulement portent
un trailer `Co-Authored-By: Claude`. Les branches restantes se répartissent en 12 `codex/`
contre 5 `claude/`, les merges nomment des branches `codex/`, et Dream tourne sur
`provider=codex` (`gpt-5.6-terra`, `gpt-5.6-sol`). **Le volume a été produit par Codex, pas
par Claude Code.** Les sessions Claude de la période étaient des audits et de l'analyse.

Deux problèmes distincts en découlent, avec deux corrections distinctes :

| Symptôme | Origine | Correction |
|---|---|---|
| 110 k lignes, 406 tests pour un gate de pins | Workflow Codex | Gate de proportionnalité (ci-dessous) |
| Sprawl de worktrees (7 sur 8 dans `.claude/`) | Sessions Claude non refermées | Hygiène de fin de session |

L'amplification côté Codex est lisible dans le nommage des branches — `round2`, `round3`,
`round4`, `round5` sur un même ticket, `attempt-1` sur d'autres : des passes successives
empilant chacune une couche, sans point de contrôle du périmètre.

Le protocole ci-dessous cadre le travail Claude. **Le gate de proportionnalité, lui, doit
contraindre en premier lieu le workflow Codex**, qui est là où le volume se produit.

| Rôle | Modèle | Quoi |
|---|---|---|
| Thread principal | Opus | Cadrage, séquencement, décisions archi, git |
| Exécution | Sonnet (`red-implementer`, `red-ops`) | Le grind, les lots mécaniques |
| Revue | Opus (`red-reviewer`) | **Aux frontières de lot uniquement**, jamais par fichier |

Trois règles dures :

1. Pas de spawn de subagent sous ~4 fichiers ou sans lecture large — inline est moins cher.
   Un spawn démarre à froid et repaie le contexte complet.
2. **Pas de jury multi-juges, pas de débat, pas de `pattern-auto`** sur cette campagne.
3. **GitNexus avant grep** pour tout travail de refactor — mais seulement une fois la
   Phase 0.1 close (cf. ci-dessous).

## Phases

### Phase 0 — Débloquer l'outillage *(bloquant pour la Phase 2)*

`CLAUDE.md` impose `gitnexus_impact` avant toute édition de symbole. Tant que l'index
ment, cette règle est un faux filet : on ne refactore pas sur une analyse d'impact fausse.

- **0.1** Purger les entrées mortes du registry GitNexus, réindexer la racine canonique,
  fixer la règle : **on indexe la racine, jamais les worktrees**. Vérifier qu'il ne reste
  qu'une entrée `brain-v42`.
- **0.2** Trier les 11 worktrees. Suppression uniquement si working tree propre **et**
  branche mergée ou commit atteignable depuis `main`. Jamais `--force`.
- **0.3** Supprimer les 14 branches mergées (`git branch -d` uniquement, jamais `-D`).
  Trier les 17 non mergées en *à intégrer* / *à abandonner* / *incertain* — **rapport
  seul, décision humaine**.

Contrainte : la chaîne `codex/70cf97a7-round2→round5` est du travail en attente
d'intégration (blocker actif : rebase/revue exacte-SHA requis). Ce n'est pas du déchet.

### Phase 1 — Remettre la production en marche

Rien de neuf n'entre dans le Brain tant que Dream est à l'arrêt.

- **1.1** Réparer Dream (ticket `d104660d`, backlog d'embeddings non comparables).
- **1.2** Flipper le killswitch ROADMAP en WET, puis traiter les 335 propositions de
  curation en attente.
- **1.3** Déployer 038 puis 039 en production selon D1 et le runbook.

### Phase 2 — ~~Dégraissage~~ · **2.1 et 2.2 ANNULÉS le 2026-08-03**

La mesure a réfuté la prémisse. **Aucun fichier de test du dépôt ne dépasse 3:1** contre son
sujet réel :

| Fichier de test | test | source | ratio |
|---|---|---|---|
| `test_container_image_pins.py` | 11 351 | 11 220 | 1,01:1 |
| `test_plan_index_repair.py` | 1 796 | 1 588 | 1,13:1 |
| `test_roadmap_curate.py` | 1 621 | 1 389 | 1,17:1 |
| `test_formatters.py` | 1 523 | 861 | 1,77:1 |
| `test_plan_index_repair_store.py` | 2 024 | 1 024 | 1,98:1 |
| `test_brain_service.py` | 1 623 | 703 | 2,31:1 |
| `test_pg_base.py` | 1 667 | 704 | **2,37:1** (maximum) |

Il n'y a pas de graisse à retirer. La phase reposait sur un ratio calculé sans son
dénominateur ; une campagne de suppression de tests sur du code sain a été évitée de peu.
Seule observation résiduelle, non bloquante : `check_container_image_pins.py` porte
**16 lignes de docstring pour 543 fonctions**, ce qui pèsera sur sa relecture — mais ne
justifie aucune suppression.

- **2.3** *(maintenu, mais réordonné)* Purger les pseudo-features `research` — **71**, dont
  64 prouvées dupliquées. À faire **après** correction du mécanisme qui les crée.

### Phase 3 — Backlog

- **3.1** *(date-bound, avant le 2026-08-22)* Déplacer `SECURITY_BURN_IN_UNTIL` par décision
  loggée, conformément à D2. Désamorce le rouge CI du 08-23 sans ouvrir la campagne sécurité.
- **3.2** Trier les 24 tickets ouverts, fermer les périmés.
- **3.3** Fermer ou abandonner les 4 items `building`, dont Sol Ultra.

## Règle anti-rechute

**Tout lot dépassant ~500 lignes de test, ou ~3× le src qu'il couvre, passe par une
validation explicite avant développement.**

C'est le garde-fou absent jusqu'ici. Sans lui, un ticket d'audit se développe jusqu'à sa
forme maximale — 406 tests pour un gate de pins — et personne ne pose la question de la
proportionnalité.

## Journal

| Date | Phase | Événement |
|---|---|---|
| 2026-08-03 | — | Bilan des 3 semaines, plan ouvert. D1 et D2 actées. |
| 2026-08-03 | 0 | Phase 0 déléguée (`red-ops`, Sonnet) : inventaire + suppressions prouvables. Triage des 17 branches remonté pour décision. |
| 2026-08-03 | 0 | 5 worktrees et 13 branches supprimés (994 → 753 MB). Registry GitNexus assaini : une seule entrée `brain-v42`, sur la racine. Index réindexé à 19 358 symboles / 37 624 relations. |
| 2026-08-03 | 0 | Patch-id : 11 des 17 branches non mergées sont déjà dans `main` sous d'autres SHA — dont toute la chaîne `70cf97a7`, contrairement au blocker Brain qui la disait en attente. Les blockers ne sont pas nettoyés quand le travail passe. |
| 2026-08-03 | 0 | `CLAUDE.md`/`AGENTS.md` : section Cross-Repo Groups restaurée **hors** du bloc `gitnexus:*`, plus la règle d'indexation. Commit `983295c8`. |
| 2026-08-03 | — | Agents `red-*` refondus : pipeline qualité automatique retiré de `red-implementer`, catégorie **Disproportion** + verdict `TOO_BIG` ajoutés à `red-reviewer`, plafond de 2 rounds, budget au dispatch. 3 bugs corrigés (`brain_session_start` interdit, clés en tirets, trailer Opus 5) et 3 erreurs factuelles dans `red-ops` (IP, container embedding, seuil VRAM). |
| 2026-08-03 | 1.1 | Cause du blocage Dream trouvée : 12 lignes sur 477 sans embedding faisaient échouer EXTRACT fail-closed. Dont 4 learnings « Dream night failure » — Dream se bloquait avec ses propres constats d'échec. Backfill 12/12 OK, backlog à 0. |
| 2026-08-03 | 1.1 | Les 18 learnings « Dream night failure » passés en `freshness_status='archived'` plutôt que supprimés : aucun outil MCP de suppression n'existe et un `DELETE` SQL aurait créé du drift PG↔Neo4j. `archived` est filtré au niveau repository ([pg_base.py:494](src/brain_v42/repositories/pg_base.py:494)), donc l'effet est réel. `updated_at` préservé en désactivant le seul trigger de timestamp. |
| 2026-08-03 | 0.3 | 11 branches supprimées après vérification patch-id à 100 % contre un index des 459 commits de `main` depuis le 2026-07-01. Reste 8 branches et 4 worktrees (contre 32 et 11 au départ), 457 MB contre 994. |
| 2026-08-03 | 3.1 | `SECURITY_BURN_IN_UNTIL` porté à 2026-09-30 (commit `c49a2654`), 66 tests verts, décision Brain `52eb2232` avec les trois alternatives écartées. |
| 2026-08-03 | 1.3 | **Preuve isolée 038→039 complète.** Backup du jour vérifié (sha256 conforme au manifest, `gzip -t` OK), restauré dans `brain_restore_test` sur `pgvector/pgvector:0.8.2-pg16` — version identique à la production, après qu'un premier essai sur le tag `pg16` ait donné pgvector 0.8.5 et fait échouer l'attestation. `alembic current = 039 (head)`, attestation `brain-v42-v4-pgrestore.sql` à **25/25 pass**. Receipt et provenance en 0600 dans `~/.local/state/brain-v42/migration-039-evidence/`. Le flag `BRAIN_ALEMBIC_ALLOW_PROD` n'a jamais été armé : le gate d'[env.py:71](alembic/env.py:71) porte sur le **nom** de la base, donc renommer la copie suffit à le laisser actif. |
| 2026-08-03 | — | **Risque DR mesuré.** Les backups (`/data/backups`) et le volume de production partagent le device `/dev/nvme0n1p3`, à 92 % d'occupation. Une perte de disque emporte les deux. Le plan DR l'exclut pourtant : « Un autre chemin du même NVMe ne compte pas. » Aggravant : la rétention est silencieusement violée — politique `max_count: 30`, réalité 83 runs et 15 GB depuis le 2026-06-12 — parce que `prune_backups` lève `ArtifactInventoryError` avant d'élaguer. Le service `red-backup` sort donc en échec chaque nuit **alors que les dumps réussissent** : l'alarme qui devrait signaler la rétention cassée est déjà devenue du bruit. L'item roadmap « Disaster recovery vérifiable » reste donc légitimement `building`. |
| 2026-08-03 | 2.3 | Roadmap mesurée : **171 features**, dont **71 `research` actives**. Sur ces 71, **64 dupliquent exactement** le nom d'un artefact existant (48 topics de learning, 16 titres de décision, zéro chevauchement) ; les 7 autres sont des messages de commit promus en features. Les chiffres du ticket `2e921e14` (58 sur 148) sont périmés — le problème a grossi depuis le 2026-07-30. |
| 2026-08-03 | 1.1 | **Le backfill est prouvé par le run Dream de 06:00.** L'erreur d'`extract` a changé de nature en base : run 860 du 02/08 « corpus dedup unavailable: corpus embedding backlog… », run 868 du 03/08 « 14 ticket(s) deferred or timed out before run deadline ». Le gate fail-closed sur le backlog ne se déclenche plus ; EXTRACT franchit la déduplication et traite réellement des tickets. Dream reste à 7/8 : la cause est désormais l'échéance d'`extract` face au volume, pas les embeddings. |
| 2026-08-03 | 1.3 | **Migration 038/039 appliquée en production**, fenêtre de 06:24. Quiescence prouvée par `pg_stat_activity` (0 connexion, 0 transaction préparée) et pas seulement par systemd. Trois gardes passées avant l'upgrade : URL littérale sur `:5433`, propriétaire du port vérifié, base à head 037 avec 2 868 learnings. Attestation live **25/25 pass**. Santé après redémarrage : `/health` ok, pool 20/0, appel MCP read-only fonctionnel. Dépôt et production enfin alignés sur 039. Les étapes 8+ du runbook n'ont pas été exécutées : elles concernent sept autres projets. |
| 2026-08-03 | 2.3 | **La pollution de la roadmap est un flux, pas un stock.** En loggant la décision `52eb2232`, une pseudo-feature `research` « Déplacer SECURITY_BURN_IN_UNTIL… » est apparue immédiatement dans la roadmap. Chaque `brain_log_decision` en crée une. Purger les 71 sans corriger ce chemin revient à vider une baignoire dont le robinet coule — et le `CLAUDE.md` encourage précisément ces captures. Corriger la promotion automatique **avant** d'archiver quoi que ce soit. |
| 2026-08-03 | 2.3 | Chemin de promotion tracé : [decision_service.py:134](src/brain_v42/services/decision_service.py:134) appelle `link_artifact_if_enabled(..., data.title)` dès qu'un embedding est stocké, puis [cluster_guard.py:105](src/brain_v42/services/cluster_guard.py:105) crée une feature quand aucune candidate ne matche. `ClusterGuard.resolve()` **n'a aucun mode « lier sans créer »**. Ce n'est pas un bug : c'est « Feature Auto-Tracking / Roadmap », statut `deployed`, qui ne distingue pas un artefact de connaissance d'un signal de travail. Le paramètre `signal_type` déjà présent dans `resolve()` est le point d'accroche naturel du correctif. |
| 2026-08-03 | 1.2 | **Flip ROADMAP en WET suspendu.** L'autorisation opérateur précédait la découverte du mécanisme de promotion. Flipper maintenant laisserait la curation nocturne appliquer ses propositions sur une roadmap dont on vient d'établir qu'elle est polluée en continu. À reconfirmer après correction. |
| 2026-08-03 | 3.3 | **Les 4 items `building` sont légitimes, aucun n'est à fermer.** DR vérifiable : rebuild Neo4j, off-host et alerting restent ouverts. Sandboxing systemd : vérifié sur les unités live, `brain-mcp-http` porte `ProtectSystem=full`/`NoNewPrivileges`/`PrivateTmp` conformément au plan, `brain-metrics` et `brain-v42-dream` n'ont rien — livraison partielle documentée. **Sol Ultra n'est pas abandonné** : c'est un méta-plan dont SA1, SEC1a, COR1, COR2, COR3, OPS1 et ARC1 lot 1 sont `done` ; il est bloqué sur ses deux constituants les plus durs, DR1 et SEC2. Les « 18 jours sans mouvement » mesurent cette dépendance, pas de la négligence. |
