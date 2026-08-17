---
title: "Brain-v42 Sol Ultra — audit remediation roadmap"
status: active
summary: "Roadmap de remédiation issue de l'audit Sol Ultra du 11 juillet 2026 : sécuriser d'abord les migrations et la récupération, puis rendre l'écriture mémoire tolérante aux pannes, fermer les incohérences métier et industrialiser le runtime."
tags:
  - audit
  - sol-ultra
  - remediation-roadmap
  - reliability
  - recovery
  - agent-security
  - pattern-auto
---

# Brain-v42 Sol Ultra — audit remediation roadmap

> Audit réalisé le 2026-07-11 sur `brain-v42`, complété par une vérification réelle de
> `ReD_v1/projects/red-backup`. Cette roadmap complète, sans la remplacer, la passe du
> [2026-07-03](2026-07-03-audit-gaps-backlog-plan.md).

## Verdict et modèle de menace

Brain-v42 est un socle mature, pas un prototype : architecture cohérente, tests nombreux,
incidents documentés et mécanismes de dégradation explicites. La note de travail issue de
l'audit est **7,5/10**. Aucun P0 ni RCE directe n'a été démontré.

Le projet reste destiné au LAN privé, sans exposition Internet, et sert des agents
personnels. Cette hypothèse baisse la priorité de l'auth multi-tenant et de l'exposition des
ports internes. Elle ne réduit pas les risques de perte de données, d'agent autorisé
compromis, de prompt injection persistée, de migration sur la mauvaise base ou de panne du
disque unique.

La stratégie retenue est une amélioration incrémentale. Une réécriture détruirait davantage
de preuves et d'invariants qu'elle n'en créerait.

## Preuves de départ

- GitNexus était aligné sur `HEAD db4caa7` pendant l'audit : 15 163 symboles, 31 473
  relations et 300 flux d'exécution.
- Le périmètre exécutable dans le sandbox a produit **2 803 tests passés**, 39 skippés,
  0 échec et **87,31 % de couverture branches**. Huit fichiers dépendant de sockets ou de
  SQLite n'ont pas été exécutés dans cet environnement.
- Ruff, format et mypy passent sur le périmètre CI déclaré. Le lint élargi aux services GPU
  trouve 7 erreurs et 3 fichiers hors format : le vert CI ne couvre donc pas tout le dépôt.
- Le dépôt contient 31 migrations au moment de l'audit. La documentation annonce encore
  plusieurs topologies et compteurs plus anciens.
- Le backup PostgreSQL de Brain existe réellement : cron quotidien à 05:00, dernier run du
  2026-07-11 à 7/7, dump de 44 951 937 octets, SHA-256 conforme, gzip valide et archive
  structurellement lisible. Les 14 derniers runs globaux sont verts ; Brain possède 16
  générations consécutives valides.
- L'audit est resté non destructif. Aucun vrai restore n'a été lancé ; la restaurabilité
  complète reste donc à prouver.

## Contrat de livraison

Cette roadmap est une source de vérité, pas un plan d'implémentation détaillé. Chaque
workstream ci-dessous devient une feature séparée au moment où il démarre.

Pour tout workstream marqué **pattern-auto requis** :

1. créer une branche dédiée et écrire un plan précis dans `docs/plans/` ;
2. faire critiquer le plan en parallèle par un juge exigences, un juge architecture et un
   juge qualité, avec au plus trois passes ;
3. exécuter en TDD par sous-agents, avec review de conformité et review qualité après chaque
   tâche ;
4. lancer les gates complets et une review de branche finale avant merge ;
5. écrire le statut réel dans la roadmap avec `brain_feature_update`.

Les changements triviaux et isolés peuvent suivre un TDD direct. Avant toute modification de
symbole, appliquer `gitnexus_impact`; avant commit, appliquer `gitnexus_detect_changes`.

## Rattachement à la roadmap existante

Ne créer une feature que si aucun bounded context existant ne porte déjà le contrat. Le plan
pattern-auto d'un chantier doit employer un titre stable, proche de la feature cible, afin
que ClusterGuard rattache ses artifacts au bon endroit.

| Workstream | Action roadmap au démarrage |
|---|---|
| SA1 | Créer `Startup fail-closed & schema compatibility gate` |
| DR1 | Créer `Disaster recovery vérifiable — PostgreSQL + Neo4j + off-site` |
| AV1 | Réutiliser `Commit-before-async: universal state safety pattern across all projects` |
| SEC1 | Réutiliser `Hawkixs runs the exact "LLM-in-prod-with-tool-access" systems its own lab-secu research flagged as unprotected — turn the threat model inward` |
| SEC2 | Traiter comme sous-scope de SEC1/OPS1 ; créer une feature seulement si le supervisor est redessiné |
| COR1 | Réouvrir `Memory Decay / Active Forgetting` |
| COR2 | Réutiliser `Design — Tickets cross-projet (coordination inter-sessions)` |
| COR3 | Réutiliser `Neo4j Knowledge Graph integration — implementation session 2026-03-16` |
| OPS1 | Réutiliser `Vérifier et réparer le CI GitLab brain_v42 après un push` |
| ARC1 | Différer ; créer une feature dédiée seulement au début du refactor |
| DOC1 | Garder comme critère de sortie OPS1, sauf chantier documentaire autonome |

## Ordre de livraison

Les vagues s'exécutent dans cet ordre. Les workstreams d'une même vague peuvent avancer en
parallèle si leurs branches et leurs preuves runtime restent isolées.

## Vague 1 — empêcher les pertes irréversibles

### SA1 — Alembic fail-closed et migrations isolées

**Priorité : P1 · pattern-auto requis**

**État au 24 juillet 2026 : livré en production.** Le plan
`docs/plans/2026-07-11-alembic-fail-closed-implementation-plan.md` a fermé les fallbacks et les
query strings ambiguës. Le cutover a ensuite vérifié le backup `20260724_104148`, appliqué 036
puis 037, exécuté `install.sh` et redémarré MCP en dernier. Les canaries schema, health,
lifecycle v4, E2E et watchdog sont vertes sur le build `be80cee`. La décision Brain
`a665e495-3a92-4a46-852d-5c90177c6e06` continue de réserver les opérations cluster au bootstrap.

Au moment de l'audit, le correctif du 30 juin avait réparé la priorité de `POSTGRES_URL` et
l'isolation des tests, mais le dernier fallback restait dangereux : `alembic/env.py`
retournait encore l'URL de `alembic.ini` si les settings étaient absents ou invalides, et
cette URL ciblait la base live `brain`.

Livrable :

- retirer tout DSN live par défaut du chemin Alembic ;
- exiger une URL explicite et valide, puis échouer avant toute connexion sinon ;
- refuser la base de production dans les drills/tests, sauf opt-in de déploiement explicite ;
- masquer credentials et DSN dans tous les logs et messages d'erreur ;
- évaluer un rôle PostgreSQL dédié aux migrations.

Preuve de sortie : tests négatifs sans env et avec env malformé, migration `upgrade head`
réussie sur une base jetable, et preuve qu'aucun chemin implicite ne résout vers `/brain`.

### DR1 — Disaster recovery vérifié, hors hôte

**Priorité : P1 · pattern-auto requis · coordination `brain-v42` + `red-backup`**

**État au 24 juillet 2026 : `building`, restore PostgreSQL courant acquis.** Le
[plan d'implémentation](2026-07-11-disaster-recovery-verified-implementation-plan.md) porte le
contrat complet et la [preuve B3](2026-07-12-disaster-recovery-b3-operational-evidence.md) est le
checkpoint actif. Sous l'autorité DR-v5, le run `20260724_150315` contient huit cibles et 47
artefacts. Il restaure PostgreSQL 16 au head 037, passe 24/24 contrôles et concorde avec une
attestation SQL indépendante. Le round-trip objet couvre 33 objets et 52 832 376 octets; le
cleanup jetable du drill est complet, le cron DR-v1 est retiré et le watchdog explicite est frais.

La production planifie encore DR-v3. DR-v5 n'a donc ni activation live ni cycle automatique
authentifié. Le restore PostgreSQL isolé au head 037 est acquis; DR1 reste ouvert pour le replay
des rôles, propriétaires et ACL, un rebuild Neo4j dédié et vide, une copie chiffrée hors domaine
de panne, une alerte reçue et le RTO complet.

Le dump PostgreSQL est sain, mais la source et `/data/backups` vivent sur le même
`/dev/nvme0n1p3`. Le pipeline n'offre donc pas de récupération après perte du NVMe.

Livrable :

- activer DR-v5 séparément et authentifier un cycle planifié, sans modifier les preuves v2/v3 ;
- copier au moins une génération chiffrée vers un autre disque ou hôte ;
- câbler les modules de chiffrement/transfert existants ou adopter une bibliothèque éprouvée,
  sans recréer un protocole de backup ;
- rejouer et vérifier les rôles, propriétaires et ACL dans la cible isolée ;
- reconstruire une projection Neo4j dédiée et vide depuis PostgreSQL et le ledger, puis comparer
  contraintes, comptes et relations ;
- mesurer le RTO complet et recevoir une alerte provoquée sur échec ou absence de succès depuis
  plus de 26 heures.

La décision option A est acquise : PostgreSQL et le ledger sont canoniques, Neo4j reste une
projection jetable. Le gate restant exige une reconstruction complète dans une base dédiée et
vide; une sauvegarde Neo4j corrélée ne peut pas le fermer.

Preuve de sortie : cycle DR-v5 planifié, récupération sur environnement vierge depuis la copie
hors hôte, checksum vérifié, rôles/ACL rejoués, schéma à head, invariants PG verts, projection
Neo4j reconstruite, alerte reçue et RTO enregistré.

## Vague 2 — garder la mémoire disponible et contenir les agents

### AV1 — Écriture PG-first malgré une panne embedding

**Priorité : P1 · pattern-auto requis**

**État au 24 juillet 2026 : implémentation livrée, dernière preuve de linking à compléter.** Les
commits `6578200`, `2e087e0`, `f29ef7b` et `b1eb53e` couvrent l'enrichissement post-commit, les
cinq types d'entité et le backfill borné. L'intégration PostgreSQL avec faux endpoint prouve la
création pendant panne, la reprise des vecteurs, FTS/vector search et un second run idempotent.
Elle n'injecte toutefois aucun linker : la preuve d'intégration des liens attendus après reprise
reste ouverte.

Au moment de l'audit, les créations de décisions, learnings, ADR, runbooks et snippets
attendaient l'embedding avant d'écrire dans PostgreSQL. Une panne GPU empêchait donc la fonction
première du produit alors que les colonnes acceptaient `NULL` et que le backfill
`--only-missing` existait déjà.

Livrable :

- persister d'abord l'entité en PostgreSQL avec `embedding=NULL` ;
- enregistrer durablement le travail de vectorisation/linking restant, via outbox ou job
  idempotent ;
- rendre la reprise observable et rejouable sans doublon ;
- conserver une recherche FTS utile pendant la dégradation ;
- exposer backlog, âge et taux d'échec des embeddings manquants.

Preuve de sortie : chaque type d'entité s'enregistre embedding coupé, reçoit son vecteur après
reprise, n'est créé qu'une fois et retrouve ses liens attendus.

### SEC1 — Capacités par agent, projet et phase Dream

**Priorité : P1 · pattern-auto requis**

**État du sous-lot webhook au 24 juillet 2026 : livré.** Les comparaisons de secret des deux
routes utilisent le temps constant depuis `20b2612`. Le commit `f73aa6e` désactive ensuite
l'auto-décompression aiohttp sur le serveur automation et centralise, après authentification,
la lecture et la décompression bornées. La pipeline GitLab `4252` est verte, 6/6 jobs, et le
ticket `d6df267c…` est clos. Le reste de SEC1 demeure ouvert sur les capacités Dream.

Le Bearer global et l'allowlist `mcp__brain-v42__*` donnent à un agent Dream autorisé des
capacités plus larges que sa phase : suppression, modification du contexte projet,
configuration de chemins de scan et écriture dans `CLAUDE.md`. Le confinement réseau ne
protège pas contre un corpus contaminé ou un agent autorisé compromis.

Livrable :

- définir des scopes par agent, projet et classe d'opération ;
- remplacer les wildcards Dream par une liste exacte par phase ;
- réserver les opérations destructives à un scope séparé ;
- borner côté serveur les racines de `plan_scan_paths` et de toute écriture filesystem ;
- prévoir rotation/révocation des tokens et audit des refus ;
- ajouter des fixtures de prompt injection persistée et des tests négatifs cross-project.

Preuve de sortie : un token Dream ne peut ni supprimer, ni écrire hors racines, ni agir sur
un autre projet, même si le contenu stocké lui demande explicitement de le faire.

### SEC2 — Services locaux bornés

**Priorité : P2 sous le modèle LAN-only · pattern-auto requis si traité comme un lot**

**État au 24 juillet 2026 : SEC2-A code-ready, non déployé.** Les commits `210660d` et
`13e53c0` bornent le shim canonique à 8 MiB/5 s, 8 lectures ingress, des lots 100/128 et un
calcul embedding + rerank par worker. Le Compose versionné lie le shim et le profil legacy à
`127.0.0.1:8003`, mais le runtime observé reste LAN-wide jusqu'à un rollout autorisé et prouvé.
Les tests ciblés sont à 61/61 et la matrice embedding/reranking élargie à 174/174, dont ces 61;
quatre revues indépendantes concluent `SHIP`.

SEC2 reste ouvert. L'authentification bearer et le réseau Docker client dédié exigent le cutover
coordonné `auto-discord` (`9ef5c69d…`). L'URL QA historique de `red-shrik` doit devenir
configurable sans changement implicite de modèle (`89140780…`). Le profil PyTorch legacy reste
non borné et ne préserve pas l'alias DNS `auto-discord`; le supervisor `deploy/dev-pc` superseded,
son ancien aiohttp et son accès Docker restent un sous-lot distinct si ce rollback est conservé.

Preuve de sortie globale : rollout avec bind effectif, bearer client/serveur atomique, réseau
dédié, tests de charge et requêtes malformées, absence de listener WAN et suppression de tout
accès Docker général sur un chemin de rollback maintenu.

## Vague 3 — fermer les incohérences métier

### COR1 — Decay alimenté par toutes les preuves d'usage

**Priorité : P1 · pattern-auto requis**

**État au 19 juillet 2026 : déployé en production via MR `!66`, commit `main` `75c6b05`
(implémentation `0bf272a`).** Les signaux remontent au parent canonique, les lectures
spécialisées alimentent le decay, le scope parent/enfant et le top-K archivé sont couverts par
trois E2E PostgreSQL. Le test live a rafraîchi le parent sans toucher les compteurs chunks.

Constat de départ : les accès aux plans sont enregistrés avec l'ID de chunk, mais `plan` manque
au registre du
`DecayFlusher`. `brain_get`, `brain_use_snippet` et l'exécution de runbooks ne produisent pas
non plus toutes les preuves prévues par le design.

Preuve de sortie : les hits remontent au plan parent, chaque lecture/exécution attendue
rafraîchit l'entité, et un test d'intégration démontre qu'une entité utilisée ne devient pas
stale à tort.

### COR2 — Transitions Ticket atomiques

**Priorité : P1 · pattern-auto requis**

**État au 19 juillet 2026 : déployé en production via MR `!66`, commit `main` `75c6b05`
(implémentation `50b38bc`).** Le CAS `id + status`, le message dans la même transaction, le
rollback réel et la course à gagnant unique sont validés sur PostgreSQL isolé puis confirmés
en live par un gagnant, un conflit stable et un seul message cohérent.

Constat de départ : la validation de l'état, l'UPDATE et l'écriture du message utilisent des
étapes
et transactions distinctes. Deux transitions concurrentes peuvent finir en last-write-wins,
ou retourner une erreur après changement effectif du statut.

Preuve de sortie : compare-and-swap ou `SELECT FOR UPDATE`, statut et message dans une seule
transaction, tests de concurrence déterministes et aucun succès partiel observable.

### COR3 — Merge raccordé au write-through graphe

**Priorité : P1/P2 · pattern-auto requis**

**État au 19 juillet 2026 : déployé en production via MR `!66`, commit `main` `75c6b05`
(implémentation `cd49067`).** Le job canonique enregistre source, cible et audit dans
PostgreSQL avant le write-through `MERGED_INTO`; les chemins admin/scoped et la dégradation
secret-safe sont validés sur PostgreSQL et Neo4j isolés. Le test live a confirmé l'audit PG et
l'arête Neo4j immédiate.

Constat de départ : `brain_merge_entities` commit directement dans PG sans passer par
`ConsolidationJob.merge`; `MERGED_INTO` peut manquer jusqu'à la réconciliation.

Preuve de sortie : une seule orchestration de merge met PG et Neo4j à jour, journalise une
dégradation éventuelle et laisse la réconciliation comme filet de sécurité, pas comme chemin
normal.

Preuve consolidée de la vague : **3 954 tests passés sur 3 954**, PostgreSQL et Neo4j de test
activés, Ruff et format sur `src tests`, mypy sur 131 fichiers source et `git diff --check`
verts. Les pipelines GitLab MR `4145` et `main` `4146` sont vertes, build registry inclus.
`brain-mcp-http` et `brain-metrics` ont redémarré sur `75c6b05`; l'E2E production COR1/2/3
est vert et toutes ses fixtures PostgreSQL/Neo4j ont été supprimées. Le rollback préparé vers
`5410c6a` n'a pas été nécessaire.

## Vague 4 — rendre chaque commit reproductible et observable

### OPS1 — Lockfile, CI et supply chain réellement appliqués

**Priorité : P2 · pattern-auto requis**

**État au 24 juillet 2026 : reproductibilité CI/Docker livrée dans `19d8cb7`; supply chain
encore ouverte.** Les jobs Python et l'image installent le graphe exact de `uv.lock` avec uv
0.10.7, le cache dépend du lockfile, Ruff couvre le dépôt complet et les tests statiques
verrouillent les commandes CI et le smoke de l'image. Le pipeline GitLab `4245` est vert, build
et smoke Docker inclus.

Le sous-lot des images opérationnelles immuables est livré sur `main` à `90d50f4`.
Le commit consommateur `921b0e1` y est fusionné, et le correctif de cycle de vie
local `c41b2b3` empêche les chemins d'upgrade et de rollback de reconstruire implicitement les
images. Le commit de fermeture `833be90` durcit le gate et la rotation Neo4j. Le gate valide
24 consommateurs; ses 1 390 tests, la matrice OPS1 de 140 tests et les 9 contrats
headless/lifecycle sont verts. Trois suites unitaires consécutives passent exactement
5 994 tests et en ignorent 48. Le CI Lint officiel GitLab répond `valid: true` avec zéro erreur
et zéro warning. Trois revues indépendantes concluent `SHIP` sans constat reproductible restant.

Le commit de livraison `90d50f4` a été poussé à l'identique sur GitHub et GitLab. La validation
post-fusion sous Python 3.12 passe 6 003 tests et en ignore 39 sur 6 042 collectés. La pipeline
GitLab `4248` est verte : 6/6 jobs, couverture 90 % et build Docker inclus. OPS1 reste ouvert
pour les modèles, les dépendances propres aux services, les paquets système, l'audit de
dépendances, le SBOM et la SAST.

Livrable : installation frozen depuis `uv.lock`, mêmes versions en CI et image, gates sur les
services omis, correction des 7 erreurs Ruff et 3 écarts de format, images et modèles immuables,
audit de dépendances et SBOM adaptés à un projet local critique. À ce stade, les images
opérationnelles sont livrées; les autres éléments de supply chain restent à livrer.

Preuve de sortie : deux builds du même commit résolvent les mêmes dépendances et le pipeline
échoue sur une régression placée dans chacun des périmètres auparavant ignorés.

### ARC1 — Séparer orchestration métier et infrastructure

**Priorité : P2 · pattern-auto requis, après les invariants métier**

**État au 24 juillet 2026 : lot 1 livré, runtime autonome encore dormant.** Les commits de
`2cf491e` à `d04dbd8` extraient webhook et déduplication dans `brain_v42.automation`, bornent les
leases et le shutdown, puis livrent une unité systemd réversible. Le propriétaire live reste le
chemin metrics legacy jusqu'au cutover séparé. `build_services()` retourne toujours un
`dict[str, Any]`, `BrainService` reçoit 15 dépendances et plusieurs handlers MCP exécutent
directement du SQL; ARC1 global reste donc ouvert.

Livrable : séparer métriques et automatisations métier, typer la composition, regrouper les
dépendances par bounded context et déplacer les requêtes hors handlers. Aucun changement de
surface MCP n'est nécessaire pour cette première passe.

Preuve de sortie : couper le sidecar métriques ne coupe plus les automatisations métier ; les
handlers testés dépendent d'interfaces typées plutôt que de SQL direct.

### DOC1 — Une seule vérité opératoire

**Priorité : P2 · TDD/direct, pas de pattern-auto si la passe reste documentaire**

**État au 23 juillet 2026 : livré dans `852c1b4` et `26a8299`, pipeline GitLab `4246` vert.**
README, CLAUDE et ARCHITECTURE reflètent le transport HTTP de production, le fallback stdio,
le endpoint reranker unifié, FastMCP 3.x et le cutover graph 035. MCP_TOOLS portait déjà les
bons compteurs. Le modèle LAN-only et son exposition GPU résiduelle sont explicites, et
`.dockerignore` est présent.

Le test `tests/unit/test_documentation_contract.py` dérive maintenant le head Alembic, les
tools enregistrés, les valeurs de configuration, le client MCP de production et la version
FastMCP lockée. Il échoue si ces contrats ou les synthèses opératoires divergent. DOC1 est clos
par la revue adversariale et le pipeline `4246`. Les chemins systemd associés publient désormais
les unités validées de façon atomique, attestent les fichiers privés sans exposer leur contenu et
imposent un canary sans course watchdog. Preuve locale : 4 607 tests passés, 289 ignorés,
matrice de revue 223/223 nominale et 223/223 sous environnement hostile, intégration systemd,
Ruff, format, syntaxe Bash et `git diff --check` verts.

Preuve de sortie : un check automatisé échoue si les compteurs ou contrats documentés
divergent du code.

## Registre historique des findings — baseline du 11 juillet 2026

Ce tableau conserve la preuve d'audit initiale ; ce n'est pas le backlog courant. Les états datés
des workstreams ci-dessus le supersèdent. Les lignes résolues ou réduites restent visibles pour
éviter de perdre leur provenance.

| Finding confirmé | Priorité ajustée | Workstream |
|---|---:|---|
| Fallback Alembic final vers le DSN live | P1 | SA1 |
| L'embedding bloque toutes les écritures mémoire | P1 | AV1 |
| Bearer global et capacités Dream wildcard | P1 | SEC1 |
| Écriture/scan filesystem sans racines serveur strictes | P1 | SEC1 |
| Embedding 8003 LAN-wide, non borné, aiohttp ancien — caps et bind cible versionnés par SEC2-A, non déployés | P2 LAN réduit | SEC2 |
| Decay sans plan parent ni preuves d'usage complètes | P1 | COR1 |
| Transitions Ticket last-write-wins et message séparé | P1 | COR2 |
| Merge MCP contournant le write-through Neo4j | P1/P2 | COR3 |
| Dump PG local sain mais sur le même NVMe | P1 | DR1 |
| Restore PostgreSQL head 037 acquis le 24 juillet ; preuve graph 035 historique, rebuild dédié et RTO off-host ouverts | P1 partiel | DR1 |
| `red-backup verify` faux vert `0/0` | P1 | DR1 |
| Cleanup/rétention peut supprimer 41 artefacts valides du dernier run | P1 | DR1 |
| Relations Neo4j rendues reconstructibles par le ledger/rebuild 035 le 22 juillet | P1 résolu | DR1 |
| Pas d'offsite, chiffrement non câblé, alerting absent | P1/P2 | DR1 |
| Cron non persistant et permissions backup trop ouvertes | P2 | DR1 |
| `uv.lock` appliqué par CI et Docker dans `19d8cb7` | P2 résolu | OPS1 |
| CI/lint dépôt complet livrés dans `19d8cb7` | P2 résolu | OPS1 |
| Images opérationnelles immuables, rotation et lifecycle livrés à `90d50f4`; pipeline `4248` verte | P2 résolu | OPS1 |
| Sidecar métriques propriétaire de fonctions métier | P2 | ARC1 |
| Composition `Any`, 15 dépendances et SQL dans handlers | P2 | ARC1 |
| Documentation et gate DOC1-A livrées ; pipeline `4246` vert | P2 résolu | DOC1 |
| `.dockerignore` présent ; modèles, dépendances, audit, SBOM/SAST encore ouverts | P2 partiel | OPS1/DOC1 |

## Registre de maintenance — audit du 23 juillet 2026

Ces tickets sont des unités de travail vérifiables, pas des features livrées. Le snapshot Brain
initial du 24 juillet ci-dessous déduplique aussi les self-tickets des runs d'audit et de
maintenance avec la roadmap ; les lignes sont ensuite maintenues à mesure des sous-lots locaux.

| Ticket Brain | État factuel | Rattachement et critère de sortie |
|---|---|---|
| `a857705f…` migrations 036/037 et restart MCP | **clos le 24 juillet**; backup `20260724_104148` vérifié 8/8 et restauré 24/24 au head 035, 036 puis 037 validées, trois unités MCP publiées, restart dernier, hardening/health/E2E/watchdog verts sur `be80cee` | SA1/runtime livré; prod au head 037, canaries abandonnées sans artifact et guards de rollback à zéro |
| `74ab1931…` DR-v5/off-host | `in_progress`; run `20260724_150315` vérifié, restore PostgreSQL 16 head 037 à 24/24, round-trip objet vert et cron v1 retiré; le timer live reste DR-v3 | DR1; activer et éprouver DR-v5, rejouer rôles/propriétaires/ACL, reconstruire Neo4j dédié, puis prouver off-host chiffré, alerte et RTO |
| `530d796a…` SEC2 embedding `:8003` | SEC2-A livré à `7508546` : 61/61 ciblés, matrice 174/174, suite 6 053/298 et pipeline `4254` verte 6/6; ticket maintenu ouvert, runtime non déployé | SEC2; rollout prouvé, auth atomique, réseau Docker dédié, charge live et traitement du legacy/supervisor |
| `9ef5c69d…` migration `auto-discord` | coordination ouverte | SEC2-B; bearer lu depuis fichier secret et réseau client dédié, avec cutover/rollback atomique |
| `89140780…` URL QA `red-shrik` | coordination ouverte | SEC2-B; rendre l'URL configurable et confirmer le propriétaire du modèle sans rediriger le trafic implicitement |
| `1460c46c…` sandbox systemd | déploiement partiel le 24 juillet : les trois unités MCP sont live et canariées (`NoNewPrivs=1`, seccomp, namespace privé, mounts credentials ro, E2E/watchdog verts); cinq fragments restent non publiés | SEC1/ARC1; rollout et canaries séparés de Dream, graph-recon et automation, sans republier MCP implicitement |
| `c1ca450c…` graph-recon legacy `--fix` | clos le 24 juillet; audit ledger read-only livré à `75ebc83`, 277/277 deploy+ledger, 60/60 docs et pipeline `4258` verte 6/6; aucun rollout live | ARC1/maintenance; writer retiré du chemin planifié, recovery 035 maintenue opérateur; rollout séparé |
| `31d68c06…` étage CI sécurité | ouvert, dédupliqué avec le reliquat OPS1 | OPS1; scanners verrouillés, politique offline/fraîcheur explicite, burn-in daté puis gate bloquant |
| `5619c851…` couverture ciblée | livraison locale terminée, revue ReD requise. Réconciliation : `9f41b01` a déjà son service et son plan `ProjectGroupTicketService`, avec une suite courante fusionnée plus complète; `b547b87` est identique pour le service, les tests, le plan et la spécification `RoadmapService`; `2f797d6`, limité aux tests, est conservé dans cette suite fusionnée. Sous-lot `pg_ticket` : message de transition partiel observé en RED puis rejeté avant session, 11/11 tests et 100 % des 60 instructions/6 branches. Sous-lot `thresholds` : cinq invariants observés en RED puis validés, 20/20 tests et 100 % des 39 instructions/16 branches. Suite unitaire : 6 256 réussis/49 ignorés | maintenance; faire relire l'intervalle Git exact par le reviewer ReD, sans merge, push ni déploiement |
| `1c6911a4…` statuts `planned` rejetés par plan_indexer | `in_progress`; code livré à `223fc1f`, pipeline `4276` verte, aucun rollout/reindex live; Brain reste à 34/35 et le plan OTLP manque | maintenance/data quality; après `44ee7643…`, déployer puis reindexer et prouver Brain 35/35 avec OTLP `archived` |
| `44ee7643…` chemins `plan_scan_paths` relatifs | ouvert; sept contextes scannent `brain_v42/docs/plans`, ce qui pollue ou vide leurs corpus | maintenance/data quality; chemins absolus canoniques, dry-run et sauvegarde, nettoyage transactionnel récupérable, puis reindex isolé avant le rollout de `223fc1f` |
| `6fcd4463…` faux vert systemd local/CI | clos le 24 juillet sans mutation; 201/201 ciblés, trois suites unitaires consécutives et pipelines `4275`/`4276` verts | maintenance; le diagnostic initial n'est plus reproductible, préserver la garde d'ancêtre de production |
| `ccb8e988…` budget tokens MCP | ouvert après audit du 24 juillet | maintenance/UX MCP; réduire les output schemas session, borner `brain_search` par item et conserver le cap roadmap all-projects |
| `45d77f10…` protocole MCP et annotations | ouvert après audit du 24 juillet | maintenance/correction; ToolAnnotations, `Literal` existants et contrat `isError`/`ToolError` testés sans changer silencieusement la surface |
| `49bda801…` profondeur JSON Python 3.14 | clos le 24 juillet; livré à `be80cee`, validations 143/143 sous Python 3.12 et 3.14, suite unitaire 6 138 réussis/48 ignorés, stall event-loop max 33,4 ms, revue `SHIP` et pipeline exacte `4261` verte 6/6 | SEC2/maintenance livré côté code; rollout/smoke 64/65 restent suivis séparément par `530d796a…` |
| `a7d85a85…` revue REORG | ouvert; 14 éléments flaggés sans mutation : 6 alertes Dream et 8 snapshots `red-shrik` | maintenance/data quality; décider et prouver requalification, purge ou extension d'allowlist pour chaque groupe |
| `8ba27dc0…` hint `reconcile_graph` périmé | clos le 24 juillet; source `83b3669` intégrée à `8c13206`, 31/31 tests et pipeline `4257` verte 6/6 | maintenance/graph; préserver le worktree source jusqu'au tri de ses changements étrangers |
| `621fcc37…` webhook constant-time | clos le 24 juillet | SEC1; deux chemins livrés à `20b2612`, 181 tests ciblés, pipeline `4250` verte et documentation des binds alignée |
| `56245929…` flakiness OPS1 | clos le 24 juillet | OPS1; trois suites unitaires consécutives vertes, validation post-fusion 6 003/39 et pipeline `4248` verte sur `90d50f4` |
| `d6df267c…` décompression webhook dédié avant auth | clos le 24 juillet | SEC1; livré à `f73aa6e`, 92 tests post-fusion, suite unitaire 6 006/48, deux revues `SHIP` et pipeline `4252` verte 6/6 |

## Réconciliation avec l'audit du 3 juillet

Baseline connue au 11 juillet : absence de restore drill, Neo4j hors backup, dérive
documentaire, CI partielle et exposition de services locaux. Depuis, le restore PostgreSQL au head
037 et le rebuild Neo4j historique au head 035 sont acquis, tout comme la reproductibilité
CI/Docker et le bornage versionné du shim canonique. L'off-host, le RTO complet, le nouveau rebuild
Neo4j dédié, la supply chain, le rollout SEC2, l'authentification, le réseau Docker dédié et le
chemin legacy/supervisor restent ouverts.

Nouveaux ou prouvés dans la baseline du 11 juillet : même NVMe, vérificateur `0/0`,
cleanup/rétention classant des artefacts valides comme orphelins, relations Neo4j alors non
reconstructibles exactement (résolu par le ledger/rebuild 035 le 22 juillet), fallback Alembic
résiduel, blocage des writes par embedding,
trous du decay, non-atomicité Ticket, merge hors write-through et modèle de capacités Dream.

Les sujets Dream déjà clos le 3 juillet — token absent et soak invalidé — ne sont pas
réouverts. Les autres sujets autonomes de cette passe restent suivis dans son plan d'origine.

## Definition of done globale

Une feature de cette roadmap n'est pas `done` sur la seule foi de mocks ou d'un test unitaire.
Elle exige :

- tests unitaires, intégration et failure injection adaptés au risque ;
- preuve runtime sur un environnement jetable ou réellement isolé ;
- rollback ou récupération documentée ;
- aucune fuite de secret dans logs et sorties d'erreur ;
- analyse GitNexus avant changement et détection des flux touchés avant commit ;
- review finale `SHIP` du diff complet ;
- statut Brain mis à jour avec la preuve de livraison.

## Prochain chantier recommandé

Le déploiement 036/037 et le sous-périmètre systemd MCP sont terminés; SEC2-A et les cinq profils
Dream/graph-recon/automation restent non déployés. La priorité opératoire DR est d'activer et
éprouver DR-v5 (`74ab1931…`), puis de fermer les gates rôles/ACL, Neo4j dédié, off-host et alerte.

La priorité data quality est `44ee7643…` : corriger les chemins multi-projets, inventorier et
nettoyer les lignes polluées, puis reindexer chaque corpus. Seulement après cette correction,
déployer `223fc1f`, reindexer Brain et prouver 35/35 avec OTLP `archived`. Sans autorité de
mutation production, le prochain lot autonome reste la dernière preuve d'intégration du linking
AV1, puis l'étage CI sécurité (`31d68c06…`) et la couverture ciblée (`5619c851…`). DR1, SEC2-B,
le reliquat SEC1, OPS1 et ARC1 restent ouverts; la roadmap globale n'est donc pas terminée.
