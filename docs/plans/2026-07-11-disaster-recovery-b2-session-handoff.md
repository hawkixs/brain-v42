---
title: "Disaster recovery vérifiable — handoff de session B2"
status: completed
summary: "Checkpoint historique B2 livré ; ne pas reprendre ses chemins ou son head 035 sur la production courante."
tags:
  - disaster-recovery
  - red-backup
  - handoff
  - pattern-auto
  - sol-ultra
---

# Disaster recovery vérifiable — handoff de session B2

> **Clôture historique — 24 juillet 2026.** B2 a été livré; ne pas reprendre les instructions de
> ce handoff. La décision Brain `3d3d72e4-acb7-49fe-aabb-1618e648e627` a adopté l'option A au
> head 035 alors déployé. La production est désormais au head 037 : toute nouvelle preuve doit
> restaurer le head exactement déployé, et aucun downgrade n'est autorisé pour suivre ce document.

> Ce checkpoint ne constitue plus un point de reprise. Les sources canoniques restent la
> [roadmap Sol Ultra](2026-07-11-sol-ultra-audit-roadmap-plan.md) et le
> [plan d'implémentation DR](2026-07-11-disaster-recovery-verified-implementation-plan.md).
> Reprendre le chantier courant depuis ces sources et le ticket DR-v3, pas depuis les branches ou
> worktrees historiques ci-dessous.

## État à reprendre

| Dépôt | Branche et checkpoint | État vérifié |
|---|---|---|
| `brain_v42` | `codex/disaster-recovery-verified`; checkpoint de code parent `39acc7df15eaf0a5a89fe983fabf8384a7ef8c26` | le commit contenant ce handoff est le cinquième commit local devant `origin/main`; aucun upstream |
| `red-backup` isolé | `/tmp/red-backup-dr1`; `codex/disaster-recovery-verified` à `a6517bfdd62e2703e001adda0cf67ccc0fb0d2c2` | propre; dix commits devant `origin/main` |
| `red-backup` principal | `/home/hawixs/hawkixs_infra/git_repo/ReD_v1/projects/red-backup`; `main` à `5328089796d4f795afd9a51445a6748c5cab320c` | propre et identique à `origin/main` |

Les quatre commits Brain antérieurs au handoff, créés localement, sont `46ca070`, `0846fbd`,
`8e3ff0f` et `39acc7d`.
Les dix commits `red-backup`, de l'ancien au récent, sont `984ce8b`, `5ba1c75`,
`19eb7d7`, `0b84ffe`, `8bc6366`, `712dbbc`, `2c021aa`, `573bf58`, `0edf3b6` et
`a6517bf`.

B1 fournit les modèles et vérifications fail-closed, la rétention non destructive, la
publication atomique des artefacts, le streaming DB, les producteurs V2 dormants,
l'autorité DR immuable et la publication exacte d'un reçu à sept targets suivi de
`.complete`. Le gate de clôture rapporte **932 tests passés, 2 ignorés et trois warnings
`AsyncMock` connus**, avec Ruff sur les fichiers Python modifiés, `git diff --check` et trois
reviews de code indépendantes `SHIP`. Le format-check n'est pas vert sur l'ensemble des
fichiers Python déjà modifiés. Aucun rapport JUnit ou git note ne persiste ces résultats : la
session B2 doit rejouer tous les gates et ne pas inférer le format depuis ce checkpoint.

Le pipeline reste dormant à ce checkpoint : `runner.py`, `config/backup.yaml` et
`deploy/systemd/*` sont identiques à `origin/main`; le CLI ne charge pas
`RecoveryAuthorityV1`; `run_all()` utilise les producteurs legacy ; les fonctions
`load_dr_v1_authority()` et `publish_completed_run_v2()` n'ont pas d'appelant de production.
`verify_run()` ne compare pas encore `receipt.policy_sha256`. Aucun déploiement n'est reflété
par les repos ou unit files visibles. Le bus systemd et `crontab -l` étant interdits dans le
sandbox, le runtime et le cron live n'ont pas été revalidés par la passe de clôture.

## B2 — prochaine tranche obligatoire

**Interdiction d'activation :** ne pas raccorder le runner ou le CLI avant que
`verify_run()` authentifie l'autorité exacte, notamment `policy_id` et `policy_sha256`, avant
d'ouvrir les manifestes de targets.

Ordre d'implémentation :

1. déplacer la validation commune de `RecoveryAuthorityV1` dans `recovery_profile.py`, tout
   en gardant une enveloppe d'erreur publique propre au writer ;
2. faire accepter l'autorité immuable à `verify_run()` et comparer exactement le policy ID
   et son SHA au reçu ; un run historique sans reçu reste `completeness=unknown`, tandis
   qu'un reçu antérieur au policy SHA est invalide ;
3. faire accepter `authority` comme argument nommé à `run_all()` et exécuter exactement les
   sept producteurs V2 autorisés ; continuer après les erreurs ordinaires de target, mais ne
   jamais capturer une annulation ou un signal de contrôle ;
4. publier le reçu puis `.complete` seulement après sept succès ; définir `all_success=True`
   uniquement après le marker durable et enregistrer séparément une erreur de run ;
5. charger l'autorité dans le CLI avant `run` et `verify-run`; tout échec de complétion doit
   sortir 1, interdire la rétention et suivre le chemin d'alerte rouge ;
6. durcir ensuite le template systemd et ses tests avec `UMask=0077`, accès SSH explicite en
   lecture seule à travers `ProtectHome=tmpfs`, et un timeout global adapté (cible actuelle :
   `TimeoutStartSec=5400`) ; ne rien installer ;
7. rejouer les failure injections, la suite complète et les reviews pattern-auto avant tout
   changement opérationnel.

Definition of done B2 : un run ne peut être vert qu'avec l'autorité exacte, sept manifests
V2 valides, un reçu canonique et son marker durable. Les erreurs ordinaires sont enregistrées
sans faux vert ; `KeyboardInterrupt`, `SystemExit` et l'annulation se propagent sans marker.
Le CLI échoue sans lancer la rétention sur toute complétion incertaine. Les anciens runs sans
reçu restent lisibles mais non attestés.

Blast radius GitNexus enregistré avant B2 : `verify_run` est **HIGH** avec 20 appelants
directs, `run_all` **MEDIUM** avec 12 appelants et `_validate_authority` **MEDIUM**. Avertir
avant de modifier `verify_run`, puis refaire `gitnexus_impact` sur chaque symbole. L'index
`red-backup` est frais à `a6517bf` (5 014 nœuds, 10 491 relations, 267 flows). L'index Brain
a été rafraîchi au parent `39acc7d` (15 347 nœuds, 31 724 relations, 300 flows) ; il sera donc
techniquement derrière le commit documentaire de handoff. Le réindexer avant toute nouvelle
édition de symbole Brain.

## Invariants à préserver

Ces changements utilisateur Brain sont hors scope. Ne pas les éditer, formatter ou stager :

| Fichier | SHA-256 attendu |
|---|---|
| `AGENTS.md` | `02a2831a24a28f4de44403a425c94aec4342da604de7b6566566f93fc90f0a21` |
| `CLAUDE.md` | `b92280a56a73c5ecc2f52b9b7b3e3d5a1540174536ae7ff767de84a8909c1a60` |
| `uv.lock` | `3728131a4dfe368004d424e29fd30068987e40dead36d95af6aa7478f78331c2` |

Le SHA-256 de leur diff Git agrégé est
`8bf616c19812ea0095a53c4831275e3579be0d9f116a1b2e01c2a0b42bdbc4a3`.
Travailler exclusivement dans `/tmp/red-backup-dr1`, jamais dans le checkout principal.
Ne pas pousser, merger, installer systemd, modifier le cron, activer cleanup/prune, toucher à
Neo4j live ni écrire vers une destination off-site sans nouvelle autorité opérateur.

Dettes P2 acceptées : `run_receipt_v2.py` reste trop grand ; des helpers privés et le loader
legacy sont fortement couplés ; une failure injection a identifié une micro-fenêtre
d'acquisition pouvant laisser un FD/temporaire privé sans créer de faux commit. Une erreur de
fermeture combinée à un signal de contrôle peut aussi être convertie en échec fail-closed.
Un callback privé arbitraire ajouté après le detach validé reste aussi hors du contrat de code
de confiance. Ces dettes ne bloquent pas B2 tant que les invariants « aucun faux reçu, aucun
faux marker » restent prouvés.

DR1 restera `building` après B2. Le restore PostgreSQL réellement isolé n'est pas encore
implémenté ; `restore_sandbox.py`, `restore_checks.py`, `restore_report.py` et `restore-drill`
sont absents. À ce checkpoint restaient aussi ouverts : preuve option A, copie chiffrée hors
domaine de panne, scheduling/alerting et permissions historiques. La preuve courante exige le
head exactement déployé (`037` actuellement), pas le head 035 de ce handoff.

## Bootstrap de la nouvelle session

```bash
cd /home/hawixs/hawkixs_infra/git_repo/brain_v42
git status --short --branch
git rev-parse HEAD
git rev-parse HEAD^
git rev-list --left-right --count origin/main...HEAD
git diff -- AGENTS.md CLAUDE.md uv.lock | sha256sum
sha256sum AGENTS.md CLAUDE.md uv.lock

git -C /home/hawixs/hawkixs_infra/git_repo/ReD_v1/projects/red-backup \
  status --short --branch
git -C /home/hawixs/hawkixs_infra/git_repo/ReD_v1/projects/red-backup \
  worktree list --porcelain

git -C /tmp/red-backup-dr1 status --short --branch
git -C /tmp/red-backup-dr1 rev-parse HEAD
git -C /tmp/red-backup-dr1 rev-list --left-right --count origin/main...HEAD
```

Les sorties attendues sont cinq commits Brain devant `origin/main`, `HEAD^` à `39acc7d`, un
worktree isolé propre, `0 10` pour la divergence `red-backup` et les quatre hashes utilisateur
exacts. Si le répertoire `/tmp/red-backup-dr1` a disparu, vérifier d'abord que la branche
locale pointe toujours sur `a6517bf`, retirer uniquement son enregistrement de worktree devenu
stale, puis recréer un worktree depuis cette branche — jamais depuis `origin/main`.

Avant toute édition de symbole : lire `AGENTS.md`, rafraîchir l'index s'il est stale et lancer
`gitnexus_impact`. Avant commit : suite complète, Ruff/format/diff-check,
`gitnexus_detect_changes`, review finale et staging explicite des seuls fichiers B2.
