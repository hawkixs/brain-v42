# Rotation coordonnée des credentials Codex gateway

Date : 2026-08-01
Ticket Brain : `52d6b319-3527-45bd-a211-058e17bfbfa9`

## But et état de départ

Le cutover de la gateway est bloqué par trois secrets encore non provisionnés de façon
coordonnée : le mot de passe PostgreSQL du rôle propriétaire `brain`, celui du rôle de lecture
`codex_ro`, et le bearer HTTP partagé entre la gateway et `red-codex`. Le dépôt possède déjà un
patron robuste pour Neo4j (`scripts/rotate_neo4j_credential.py`), mais le runbook gateway ne
fournit qu'une suite d'opérations manuelles.

Le mécanisme doit couvrir les consommateurs réellement observés :

- `brain_v42/.env` pour le MCP HTTP, les automations et la gateway Compose ;
- l'accès Dream, indirectement via le MCP HTTP, et son timer pendant la quiescence ;
- `red-data/.env` pour ses deux services Dagster ;
- `/etc/shrik/env` pour le daemon `red-shrik` ;
- `red-codex/.env.local` pour `codex_ro`, l'URL privée `:9211` et le bearer ;
- `~/.config/brain-v42/codex-gateway.env` pour le bearer côté gateway.

Modifier ces fichiers privés et recréer leurs services est une opération de déploiement. Aucun
fichier source de `red-codex`, `red-data` ou `red-shrik` n'appartient au diff `brain_v42`. Le
défaut d'affichage des tickets reste donc une coordination séparée côté `red-codex`.

## Options

### Option 1 — coordinateur fail-closed à inventaire fixe (retenue)

Ajouter un CLI Python dans `brain_v42`, sec et lecture seule par défaut. L'opérateur lui fournit
les racines canonisées de `brain_v42` et de ReD ; le programme en déduit une liste fermée de
fichiers, services et clés. Il refuse un chemin inattendu, un lien symbolique, un doublon de clé,
un fichier trop permissif non corrigeable, un consommateur absent ou une commande de préflight
en échec.

Le mode `--apply` :

1. prend un verrou exclusif et écrit un journal reprenable `0600` dans le répertoire privé ;
2. génère trois secrets distincts sans les placer dans les arguments, l'environnement, Git ou
   les sorties ;
3. prépare les fichiers complets puis quiesce Dream/MCP/automations, Dagster, Shrik,
   `red-codex` et une éventuelle gateway ;
4. change `brain` et `codex_ro` dans une seule transaction PostgreSQL locale ;
5. installe atomiquement les fichiers privés et recrée seulement les consommateurs concernés ;
6. exige, sur de nouvelles connexions, les deux nouveaux mots de passe acceptés, les deux
   anciens explicitement refusés, les privilèges bornés de `codex_ro`, `/ready` vert, le nouveau
   bearer accepté et l'ancien refusé ;
7. supprime le journal uniquement après toutes les preuves.

Une erreur déclenche le rollback : arrêt des consommateurs redémarrés, transaction PostgreSQL
inverse, restauration atomique des contenus privés et remise dans leur état d'activité initial.
Si le rollback ne peut pas être prouvé, le journal reste présent et le CLI échoue avec un message
générique ; il n'essaie jamais de poursuivre le cutover.

Avantages : répétable, testable avec des frontières simulées, secrets absents des logs, fenêtre
de coupure bornée, résultat JSON sans secret. Inconvénient : le host doit autoriser à l'avance
les opérations Docker et le `sudo -n` strictement nécessaire à `/etc/shrik/env` et au service
Shrik.

### Option 2 — runbook manuel enrichi

Documenter l'ordre de rotation, garder les secrets dans des saisies masquées et demander à
l'opérateur de modifier puis redémarrer chaque consommateur. Cette option demande moins de code,
mais elle ne peut pas prouver l'exhaustivité des fichiers modifiés, reprendre une interruption,
ni tester automatiquement le rollback. Une erreur entre `ALTER ROLE` et le dernier fichier peut
laisser plusieurs projets avec des générations différentes.

Elle est rejetée pour ce cutover : le gain de code ne compense pas l'absence de transaction
opérationnelle et de preuve reproductible.

## Interface bornée

Le CLI accepte uniquement des entrées non secrètes : racines absolues, répertoire privé,
`--apply`, `--resume` ou `--rollback`, et confirmations opérateur explicites. Aucun argument de
mot de passe ou bearer n'existe. Les commandes externes sont codées dans le programme ; aucun
shell ni commande arbitraire ne vient d'un manifeste.

Le dry-run obligatoire valide au minimum :

- propriétaires, types et modes des six fichiers privés ;
- clés attendues, rôles, hôtes, ports et base dans les DSN, sans rendre leurs valeurs ;
- `docker compose config --quiet` dans les trois projets ;
- disponibilité de `systemctl --user`, de Docker, de PostgreSQL et de `sudo -n` pour Shrik ;
- état Alembic exactement `037`, dix vues, sept barrières et deux triggers ;
- port gateway exactement `9211`, sans publication hôte.

Le résultat ne contient que des booléens, compteurs, identifiants de consommateurs et états
sanitisés. Les exceptions capturées ne sont jamais chaînées vers la sortie opérateur.

## TDD et lots atomiques

1. Tests du contrat sec : dry-run par défaut, inventaire fermé, modes, doublons, DSN, aucune
   entrée secrète dans le parseur ou les sorties.
2. Tests de la machine de cutover : journal/verrou, ordre quiesce-rotation-install-restart,
   nouvelles connexions, anciens refus et privilèges `codex_ro`.
3. Tests de chaque panne injectée : rollback base/fichiers/états, journal conservé si rollback
   incomplet, absence de secret dans erreurs et appels externes.
4. Documentation du CLI et remplacement des recettes manuelles du runbook.

Le cutover live reste interdit avant un reviewer frais `APPROVE`, un tester frais `PASS` sur le
même HEAD et un dry-run réel avec rollback préflight vert. Le mécanisme ne migre jamais Alembic :
la production observée reste à `037` pour ce ticket, même si le dépôt est à `039`. Les révisions
038 et 039 restent hors de ce cutover et ne sont jamais appliquées implicitement.

## Maillon externe éventuel

Le seul maillon potentiellement humain est l'absence d'une autorisation non interactive déjà
provisionnée pour écrire `/etc/shrik/env` et piloter `red-shrik.service`. Le coordinateur doit le
détecter au dry-run avant toute mutation. Aucun mot de passe sudo ne sera demandé, stocké ou
inventé ; si `sudo -n` est indisponible, tout le reste peut être livré et ce prérequis précis reste
le seul blocage du cutover live.

## Contrat privilégié corrigé

Le préflight appelle exactement
`sudo -n /usr/local/sbin/brain-shrik-env-control --check`. L'apply et le rollback écrivent leur
payload dans le staging fixe privé
`~/.config/brain-v42/codex-gateway-rotation/.shrik-env.install`, puis appellent exactement le même
`--publish`. Le helper root-owned refuse toute cible, unité ou action libre; il publie uniquement
`/etc/shrik/env` en `root:hawixs 0640` par remplacement atomique, fsync et relecture complète.

Le helper borne aussi `--stop`, `--start` et `--is-active` à `red-shrik.service`. Le sudoers
versionné énumère ces cinq argv exacts. Les anciens grants `tee` et `systemctl red-*` de red-shrik
ne constituent pas une preuve pour ce cutover; aucun grant `true` ou `install` n'est ajouté.
L'unique geste root initial est `sudo ./deploy/install-brain-shrik-env-control.sh` depuis le
checkout revu. Cet installeur ne modifie ni `/etc/shrik/env` ni l'état du service.
