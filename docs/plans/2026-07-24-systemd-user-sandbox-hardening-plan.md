# Sandboxing des unités systemd utilisateur — plan d'implémentation

**Date :** 2026-07-24
**Ticket Brain :** `1460c46c-0386-44b9-bbed-9ce45c3c5483`
**Branche :** `feat/systemd-sandbox-hardening`
**Statut :** code-ready, trois revues finales SHIP, non déployé
**Déploiement :** interdit dans ce lot

## Objectif

Réduire la surface d'exploitation des unités systemd utilisateur gérées par brain-v42 sans
casser leurs contrats applicatifs. Le lot doit aussi retirer deux fausses assurances : le profil
filesystem actuel d'automation n'est pas effectif sous systemd 249 sans `PrivateUsers=true`, et
`install.sh --dry-run` publie malgré son nom des unités dans le répertoire systemd utilisateur.

La livraison attendue est **code-ready uniquement** : templates, rendu isolé, tests, documentation
et runbooks. Aucun fichier sous `~/.config/systemd`, `daemon-reload`, enable, start, stop, restart,
timer ou secret live ne sera modifié.

## Faits établis

- L'hôte utilise systemd 249 et les unités concernées sont des services `--user`.
- Le manuel local précise que les protections nécessitant un namespace de montage, dont
  `ProtectSystem`, `ProtectHome`, `PrivateTmp` et `ReadWritePaths`, ne deviennent utilisables dans
  une unité user qu'avec `PrivateUsers=true`.
- Le noyau autorise les user namespaces et leur imbrication, mais cela ne prouve pas la
  compatibilité d'un vrai run Codex/Claude sous Dream.
- Le runtime observé charge :
  - MCP HTTP actif sans sandbox ;
  - Dream inactif entre deux runs, sans sandbox ;
  - graph-recon inactif et jamais prouvé, sans sandbox ;
  - automation inactive avec des directives filesystem configurées mais sans `PrivateUsers`.
- MCP HTTP écoute en loopback et sort vers PostgreSQL, Neo4j et les services embedding/reranking.
  Il lit des chemins de plans dynamiques et peut écrire des sections `CLAUDE.md` configurées ;
  ce dernier comportement ne doit pas être cassé silencieusement par ce lot.
- Graph-recon n'écrit pas sur le filesystem : il lit le dépôt et `.env`, puis écrit uniquement
  dans PostgreSQL/Neo4j. Son login shell est inutile car l'interpréteur est absolu et `Settings`
  charge `.env` depuis le `WorkingDirectory`.
- Le gestionnaire user ne fournit pas `network-online.target`. Dream et graph-recon conservent
  pourtant `After/Wants=network-online.target` : cette relation n'ordonne rien. Dream possède déjà
  un preflight MCP explicite ; graph-recon doit échouer normalement si ses bases sont indisponibles.
- Dream écrit sous `logs/dream`, utilise le runtime temporaire, lance `uv`, Python, Codex et un
  rollback Claude, et dépend de caches/authentifications sous HOME. Son sandbox enfant peut créer
  des namespaces, utiliser Landlock et seccomp.
- Le watchdog lance `curl` puis `systemctl --user restart`; il reste une unité exécutable et doit
  recevoir au moins un profil réduit compatible avec le bus utilisateur.

## Décisions de profil

### Baseline forte d'intégrité : automation et graph-recon

Ces services Python n'ont pas de sous-processus sandboxé ni d'écriture filesystem légitime :

```ini
UMask=0077
NoNewPrivileges=true
PrivateUsers=true
PrivateTmp=true
PrivateDevices=true
ProtectSystem=strict
ProtectHome=read-only
ProtectClock=true
ProtectControlGroups=true
ProtectKernelLogs=true
ProtectKernelModules=true
ProtectKernelTunables=true
CapabilityBoundingSet=
AmbientCapabilities=
KeyringMode=private
LockPersonality=true
RestrictAddressFamilies=AF_UNIX AF_INET AF_INET6
RestrictRealtime=true
RestrictSUIDSGID=true
SystemCallArchitectures=native
```

Graph-recon passe en `ExecStart` Python direct. Aucun `ReadWritePaths` n'est ajouté : les sorties
restent dans journald. `ProtectHome=read-only` protège l'intégrité, pas la confidentialité : les
clés SSH, credentials d'agents et autres dépôts du même utilisateur restent lisibles et le réseau
autorisé peut les exfiltrer. Un futur profil `ProtectHome=tmpfs` avec binds minimaux doit d'abord
résoudre le véritable interpréteur ciblé par le symlink `.venv/bin/python`.

### Baseline forte compatible écriture HOME : MCP HTTP

MCP reçoit la même baseline, mais `ProtectSystem=full` remplace `strict` et `ProtectHome` est omis.
Il ajoute `ReadOnlyPaths=__REPO_ROOT__/.env %h/.config/brain-v42` pour empêcher la modification des
configurations et credentials Brain. Ce choix conserve les écritures `CLAUDE.md` configurées et
les chemins projet dynamiques. Il rend `/usr`, `/boot`, `/efi`, `/etc`, `.env` et la configuration
Brain read-only, isole `/tmp` et les devices, retire les capacités et borne les familles de sockets,
mais **ne confine pas le reste de HOME**. Le bornage serveur des racines de scan et d'écriture reste
le reliquat SEC1c ; la documentation ne présentera pas ce profil comme une protection de
confidentialité des secrets utilisateur.

### Candidat réduit non déployable avant canary : Dream et watchdog

Ces unités reçoivent uniquement les restrictions qui ne nécessitent pas de namespace de montage :

```ini
UMask=0077
NoNewPrivileges=true
LockPersonality=true
RestrictRealtime=true
RestrictSUIDSGID=true
SystemCallArchitectures=native
```

Dream ne reçoit pas encore `PrivateUsers`, `PrivateTmp`, `ProtectSystem`, `ProtectHome`,
`RestrictNamespaces`, `MemoryDenyWriteExecute`, `CapabilityBoundingSet` vide ou filtre
`SystemCallFilter` ni `RestrictAddressFamilies`. Ces directives exigent un canary réel Codex
**et** Claude et/ou risquent de casser le sandbox imbriqué, Node/libuv, les caches, le token refresh
ou le lock partagé. Le watchdog ajoute, lui, `RestrictAddressFamilies=AF_UNIX AF_INET AF_INET6` :
curl loopback et le bus `systemctl --user` n'ont besoin d'aucune autre famille.

### Filtres différés

`SystemCallFilter=@system-service`, `SystemCallFilter=~@mount ...` et
`RestrictNamespaces=true` ne sont pas ajoutés dans ce lot. `systemd-analyze verify` ne peut pas
prouver leur compatibilité avec asyncpg, Neo4j, uv, Codex ou Claude. Ils ne pourront être activés
qu'après un trace/canary réel et un rollback connu bon.

## Rendu et vérification sans publication live

Ajouter `install.sh --check-only` avec les invariants suivants :

- incompatible avec `--dry-run` et `--uninstall` ;
- rend les huit fichiers gérés dans un `mktemp` borné sous `/tmp` ;
- exécute d'abord les preflights host-aware sans afficher leurs valeurs, puis lance séparément le
  verifier **une seule fois sur les huit fichiers** avec `HOME` et les XDG temporaires vides, un
  `SYSTEMD_UNIT_PATH` limité au staging et aux cinq répertoires d'unités vendor, ainsi que
  `--generators=no --man=no` ;
- ne crée pas `USER_UNIT_DIR`, n'inspecte pas les unités live, ne publie aucun fichier et
  n'appelle jamais `systemctl` ;
- nettoie son répertoire temporaire par un chemin explicitement validé ;
- journalise explicitement `check-only: no managed units changed`.

Ajouter aussi `install.sh --render-dir /chemin/absolu/neuf` :

- incompatible avec les trois autres modes ;
- exige un parent existant, répertoire, possédé par l'utilisateur ; résout ce parent avec
  `realpath -e`, exige `u+wx`, refuse `g+w`/`o+w`, tout composant symlinké ainsi qu'une cible
  canonique sous `USER_UNIT_DIR`, et refuse une cible relative, existante ou symlinkée ; chaque
  conteneur ancêtre doit appartenir à l'UID racine effectif ou à l'utilisateur courant, avec
  sémantique sticky sûre lorsqu'il est inscriptible ;
- applique le même rendu, les mêmes preflights host-aware et la même vérification hermétique ;
- crée un staging privé frère, exige exactement huit fichiers réguliers sans placeholder, puis
  publie par un unique `mv -T --no-clobber` vers la cible finale mode `0700` ; l'identité
  `device:inode` du parent, du staging et du rendu est gardée et revalidée ;
- sur échec ou signal, supprime uniquement une cible qui porte encore l'identité du rendu ; une
  cible remplacée concurrentement est préservée et signalée, et le staging est nettoyé seulement
  s'il conserve sa propre identité ;
- publie ainsi **dans ce répertoire de sortie seulement**, sans lire ou écrire une unité live et
  sans `systemctl` ;
- fournit ainsi un artefact inspectable dont l'opérateur peut installer un seul basename exact.

Les deux modes échouent fermés si `systemd-analyze` est absent ou si un seul fichier ne passe pas
`verify` : retour non nul, staging nettoyé et aucun `--render-dir` publié.

Le cleanup ne fait jamais de `stat` puis `rm -rf` sur la cible publique : il la remet d'abord par
renommage atomique dans le slot privé du staging, vérifie son identité, et restaure tout
remplacement concurrent. Le parent privé et le sticky bit de `/tmp` protègent contre les autres
UID. Ce contrat Bash ne revendique pas une isolation contre un processus hostile du **même UID**,
qui peut encore courir les opérations path-based ; cette frontière exigerait un helper fondé sur
des `dirfd`, `renameat2(RENAME_NOREPLACE)` et `unlinkat`.

Le comportement historique de `--dry-run` reste compatible : rendu et publication atomique dans
`USER_UNIT_DIR`, sans `systemctl`. Les runbooks doivent cesser de le décrire comme sans effet de
bord et utiliser `--check-only` pour une vérification éphémère ou `--render-dir` pour préparer un
artefact de rollout hors du répertoire systemd.

Un sélecteur de publication live reste hors de ce lot. La fenêtre opérateur devra sauvegarder les
fragments/drop-ins, produire un `--render-dir`, puis copier atomiquement un seul basename validé et
canarier graph, MCP et Dream séparément. L'installation normale et `--dry-run` ne doivent pas être
utilisés comme rollout global aveugle.

## Cycle TDD

### RED 1 — cohérence des namespaces user

Créer un contrat central des templates `.service.tmpl` qui échoue si une unité contient une
directive de namespace/montage (`PrivateTmp`, `PrivateDevices`, `PrivateMounts`, `ProtectSystem`,
`ProtectHome`, `ProtectClock`, `ProtectControlGroups`, `ProtectKernelLogs`,
`ProtectKernelModules`, `ProtectKernelTunables`, `ProtectProc`, `ProcSubset`, `ReadWritePaths`,
`ReadOnlyPaths`, `InaccessiblePaths`, `BindPaths`, `BindReadOnlyPaths`, `TemporaryFileSystem`,
`NoExecPaths`, `ExecPaths`, `PrivateNetwork`, `PrivateIPC`, `ProtectHostname`, `RootDirectory`,
`RootImage`, `MountAPIVFS`, `MountImages`, `ExtensionImages`) sans `PrivateUsers=true`. Le test
doit échouer sur automation.

### RED 2 — profils exacts par unité

Tester les quatre services principaux et le watchdog :

- occurrence unique de chaque directive ;
- aucune valeur `false` ou override contradictoire ;
- baseline d'intégrité exacte pour automation/graph ;
- baseline `ProtectSystem=full` sans `ProtectHome` et avec config Brain read-only pour MCP ;
- baseline réduite exacte et absence explicite des directives différées pour Dream/watchdog ;
- familles réseau limitées à `AF_UNIX AF_INET AF_INET6` pour les profils forts et le watchdog,
  mais différées explicitement pour Dream ;
- graph-recon utilise l'interpréteur absolu sans `/bin/bash -lc` ; Dream et graph-recon ne
  revendiquent plus le `network-online.target` système absent du user manager.

### RED 3 — vrai check-only et render-dir isolé

Étendre les tests de l'installateur avec un faux HOME/XDG et des wrappers hostiles :

- `returncode == 0`, message exact de succès et aucun `systemctl` appelé ;
- `USER_UNIT_DIR` reste absent et une sentinelle hostile n'est ni lue ni scannée ;
- les huit basenames exacts existent au moment du verify, sans placeholder, puis le staging du
  check-only est supprimé ;
- le wrapper du verifier reçoit les `HOME`/XDG temporaires, le chemin systemd isolé et les options
  sans générateur/man ; une valeur-secret sentinelle de fixture est absente de stdout/stderr sur
  les succès comme sur toutes les erreurs ;
- les combinaisons de flags sont refusées avant mutation ;
- succès, erreur de rendu, erreur de verify, `INT` et `TERM` nettoient le staging sans toucher les
  unités existantes ; l'absence de `systemd-analyze` échoue non-zéro avant toute publication ;
- `--render-dir` refuse aussi un parent symlinké vers `USER_UNIT_DIR`, rend dans un staging frère
  puis renomme atomiquement ; la cible finale reste absente après erreur, `INT` ou `TERM`, et le
  succès conserve exactement les huit artefacts validés ;
- le chemin `--dry-run` historique reste couvert séparément.

### GREEN minimal

Modifier uniquement les cinq templates, `install.sh`, les tests contractuels et les runbooks.
Ne modifier ni le code applicatif MCP/Dream, ni les secrets, ni les unités live.

## Fichiers attendus

- `deploy/systemd/brain-mcp-http.service.tmpl`
- `deploy/systemd/brain-mcp-http-watchdog.service.tmpl`
- `deploy/systemd/brain-v42-automation.service.tmpl`
- `deploy/systemd/brain-v42-dream.service.tmpl`
- `deploy/systemd/brain-v42-graph-recon.service.tmpl`
- `deploy/systemd/install.sh`
- `deploy/systemd/README.md`
- `deploy/systemd/MCP_HTTP_RUNBOOK.md`
- `tests/unit/deploy/test_systemd_sandbox_profiles.py` (nouveau)
- tests installateur existants au strict nécessaire
- `tests/integration/test_dream_systemd_install.sh`
- roadmap et ce plan pour la preuve finale

## Gates avant fusion

```bash
pytest -q \
  tests/unit/deploy/test_systemd_sandbox_profiles.py \
  tests/unit/deploy/test_mcp_http_unit.py \
  tests/unit/deploy/test_automation_unit.py \
  tests/unit/deploy/test_systemd_ci_portability.py

bash -n deploy/systemd/install.sh
shellcheck deploy/systemd/install.sh tests/integration/test_dream_systemd_install.sh
REQUIRE_SYSTEMD_ANALYZE=1 bash tests/integration/test_dream_systemd_install.sh
ruff check .
ruff format --check .
mypy src/
pytest -q
git diff --check
```

Le smoke shell doit appeler `--check-only` dans sa fixture HOME/XDG et vérifier les huit artefacts
avec le vrai parseur systemd. Le verifier peut effectuer des lectures système read-only ; la
garantie est l'absence de lookup HOME/XDG local, de mutation lifecycle et de tout `systemctl`.

## Preuve runtime différée

Le ticket reste `in_progress` après fusion tant qu'une fenêtre opérateur n'a pas prouvé, unité par
unité :

1. backup persistant du fragment et des drop-ins connus bons ;
2. depuis le checkout canonique de production au SHA validé, rendu `--check-only`, puis
   `--render-dir` ; copie d'un seul basename via fichier `.new` et
   `mv` atomique dans `USER_UNIT_DIR`, `systemctl --user daemon-reload`, sans activation globale
   des timers ;
3. propriétés déclarées par le manager via `systemctl --user show` et score heuristique
   `systemd-analyze security` ; ces deux lectures ne sont jamais présentées comme une preuve
   d'enforcement sur le processus ;
4. preuve d'enforcement sans secret : pour les profils forts, probe transient portant les
   directives exactes du fragment rendu, écriture autorisée dans une fixture et hors périmètre
   refusée ; pour les profils forts et le watchdog, famille de socket exotique refusée. Sur tout
   processus persistant redémarré, vérifier aussi `NoNewPrivs: 1` dans `/proc/$PID/status` et les
   montages attendus en lecture seule dans `/proc/$PID/mountinfo`. Dream consigne ces protections
   comme résiduelles jusqu'à son canary dédié ;
5. graph-recon en rapport sans `--fix` avant tout run mutateur ;
6. MCP : neutraliser d'abord le watchdog, capturer l'ancien `MainPID`, publier le fragment,
   `daemon-reload`, puis `restart` ; exiger un nouveau `MainPID` non nul et différent avant toute
   validation. Vérifier ensuite les preuves d'enforcement du point 4, le health, les appels
   authentifiés lecture et écriture, DB/Neo4j/embedding et l'écriture d'un `CLAUDE.md` de fixture
   sauvegardé/restauré. Tout échec déclenche le rollback immédiat avant de réarmer le watchdog ;
7. Dream ne démarre pas son unité complète : `dream.sh --dry-run` conserve des chemins mutants.
   Canarier directement `codex_runner`, puis le chemin Claude séparé, sous une unité transient au
   même profil et avec des outils MCP lecture seule. Le canary complet reste bloqué jusqu'à un
   vrai mode applicatif sans scrub `--live`, WET EXTRACT/ROADMAP ni alerte ; automation est validée
   selon son contrat de lease/cutover ;
8. rollback adapté : MCP restaure/reload/restart puis health ; Dream/graph restaurent avant le
   prochain timer sans relancer le oneshot ; automation respecte la lease ; watchdog est restauré
   puis probé sans provoquer de restart. Ne jamais utiliser `install.sh --uninstall`.

## Critère de sortie code-ready

- Les profils versionnés sont exacts, différenciés et ne revendiquent pas plus que leurs garanties.
- Toute protection filesystem d'une unité user exige `PrivateUsers=true` par test.
- `--check-only` ne publie rien ; `--render-dir` ne sort que des artefacts vérifiés hors systemd.
- Les gates complètes et trois revues indépendantes concluent `SHIP` sans finding P0–P3.
- Le ticket Brain reçoit les commits, tests et limites ; il reste ouvert jusqu'à preuve runtime.

## Preuve code-ready du 24 juillet 2026

- Commits du lot : plan `dea89c9`, contrats RED `4c1a650`, implémentation GREEN `0e29044` ;
  le commit documentaire final porte les runbooks, l'architecture et la roadmap.
- RED initial : 56 contrats, 35 échecs attendus et 21 succès ; cinq régressions supplémentaires
  ont ensuite été ajoutées depuis les findings de revue (symlink live, permissions/ancêtres,
  cleanup, remplacement concurrent et échec de sortie après publication).
- GREEN final : 61/61 contrats profils + installateur, 228/228 tests deploy et smoke systemd 249
  avec 29 checks verts ; seule la probe du manager user est ignorée faute de bus disponible.
- Gates dépôt dans l'environnement CI Python 3.12.12 : Ruff sur 600 fichiers, Mypy sur 162
  fichiers, 6 114 tests passés et 298 ignorés ; syntaxe Bash, ShellCheck et `git diff --check`
  verts.
- Trois revues indépendantes (profils, sécurité/TOCTOU et qualité/compatibilité) concluent
  `SHIP`, sans finding P0, P1, P2 ou P3.
- Aucun fragment live, drop-in, secret, timer, manager systemd, service, base ou conteneur n'a été
  modifié. Le ticket reste `in_progress` jusqu'aux canaries et preuves d'enforcement opérateur.
- Diagnostic de maintenance séparé : Python 3.14 accepte le JSON imbriqué utilisé par un contrat
  SEC2 là où Python 3.12 le rejette par récursion. La CI 3.12 reste verte ; cette compatibilité ne
  doit pas être corrigée dans le lot systemd.
