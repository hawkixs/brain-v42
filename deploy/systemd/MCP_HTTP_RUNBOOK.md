# MCP HTTP systemd — runbook opérateur

Ce runbook couvre le service partagé `brain-mcp-http.service` et son watchdog. Le chemin
d'installation normal génère et valide les unités sans modifier leur état. Leur démarrage,
leur activation au boot et leur arrêt restent des décisions opérateur.

The production systemd contract is fixed to `127.0.0.1:8765`, comme le client `.mcp.json`
versionné.
Un override `Settings.mcp_http_port` reste possible pour le développement hors de ce chemin,
mais l'installateur et le service de production refusent toute autre valeur afin que serveur,
clients, healthchecks et watchdog ne puissent pas diverger.

> **Portée destructive de la désinstallation.** `deploy/systemd/install.sh --uninstall`
> arrête, désactive et supprime **toutes** les unités gérées par le script : MCP HTTP,
> watchdog, Dream, graph-recon et automation. Ne pas l'utiliser comme simple rollback MCP.
> La combinaison `--dry-run --uninstall` est rejetée sans effet de bord.

Les commandes ci-dessous s'exécutent depuis la racine du checkout destiné à la production.
Ne jamais copier une valeur de secret dans un journal ou une preuve partagée.

### Render path terminology

`render_parent` is the private, pre-existing parent directory that contains the rendered
artifacts and any backup directory. `render_dir` is the new child directory created inside it
for the generated unit files; it is not the parent and it must not exist before rendering.
For example, `/state/systemd-render.ABC123` is `render_parent` and
`/state/systemd-render.ABC123/units` is `render_dir`. The installer checks the parent ancestry
for canonical, user-owned, non-replaceable path components before creating the child.

## Upgrade d'une unité existante

Les anciennes installations pouvaient conserver `MCP_HTTP_TOKEN` dans le `.env` du checkout.
Le nouveau preflight bloque **avant toute régénération** tant que cette duplication existe :
le prochain restart ne peut donc pas basculer silencieusement vers une configuration invalide.

Avant `install.sh`, vérifier la situation sans afficher la valeur :

```bash
set -euo pipefail
repo_root="$(pwd)"
private_token="$HOME/.config/brain-v42/mcp-token.env"
test -f "$private_token"
grep -Eq '^[[:space:]]*MCP_HTTP_TOKEN=.+$' "$private_token"
if grep -Eqi '^[[:space:]]*(export[[:space:]]+)?MCP_HTTP_(TOKEN|DREAM_TOKENS)[[:space:]]*=' \
  "$repo_root/.env"; then
  echo 'MIGRATION REQUIRED: remove private MCP keys from the shared .env' >&2
  exit 2
fi
```

Si la gate signale une migration, sauvegarder `.env` dans un emplacement privé `0700/0600`,
confirmer en privé que `mcp-token.env` porte le bearer courant, puis retirer **uniquement** les
affectations `MCP_HTTP_TOKEN`/`MCP_HTTP_DREAM_TOKENS` du `.env` avec un éditeur local. Ne pas
mettre la valeur dans l'historique shell, un diff ou un ticket. Relancer ensuite le bloc ci-dessus
et le preflight complet. Ce dépôt ne migre ni ne supprime automatiquement le secret de l'hôte.

## Preflight

Vérifier d'abord le checkout, les fichiers privés et les huit unités sans toucher au répertoire
systemd live avec `--check-only`. `--render-dir` produit ensuite un artefact privé hors systemd.
Le bloc sauvegarde et publie uniquement les trois fragments MCP après neutralisation explicite du
watchdog, puis recharge le manager. Cette neutralisation est la première mutation de lifecycle ;
le watchdog ne sera réactivé qu'après le go/no-go.

Ne pas remplacer cette séquence par `--dry-run` : ce mode historique écrit les huit unités gérées
dans `${XDG_CONFIG_HOME:-$HOME/.config}/systemd/user`, même s'il n'appelle pas `systemctl`.

```bash
set -euo pipefail
repo_root="$(pwd)"
user_unit_dir="${XDG_CONFIG_HOME:-$HOME/.config}/systemd/user"
evidence_root="${XDG_STATE_HOME:-$HOME/.local/state}/brain-v42-mcp-upgrade"
mkdir -p "$evidence_root"
chmod 0700 "$evidence_root"
render_parent="$(mktemp -d "$evidence_root/systemd-render.XXXXXX")"
render_dir="$render_parent/units"
backup_dir="$render_parent/live-backup"
mkdir -p "$backup_dir"
chmod 0700 "$render_parent" "$backup_dir"
test -x "$repo_root/.venv/bin/python"

"$repo_root/.venv/bin/python" "$repo_root/scripts/check_graph_projector_env.py" \
  --shared "$repo_root/.env" \
  --private "$HOME/.config/brain-v42/graph-projector.env"
"$repo_root/.venv/bin/python" "$repo_root/scripts/check_mcp_http_port.py" \
  --shared "$repo_root/.env" --expected 8765 --expected-host 127.0.0.1 \
  --token-file "$HOME/.config/brain-v42/mcp-token.env"

deploy/systemd/install.sh --check-only
deploy/systemd/install.sh --render-dir "$render_dir"
for unit in \
  brain-mcp-http.service \
  brain-mcp-http-watchdog.service \
  brain-mcp-http-watchdog.timer; do
  test -f "$render_dir/$unit"
done
systemctl --user stop brain-mcp-http-watchdog.timer
systemctl --user stop brain-mcp-http-watchdog.service
systemctl --user disable --no-reload brain-mcp-http-watchdog.timer
mkdir -p "$user_unit_dir"
for dropin_dir in \
  brain-mcp-http.service.d \
  brain-mcp-http-watchdog.service.d \
  brain-mcp-http-watchdog.timer.d; do
  if [[ -d "$user_unit_dir/$dropin_dir" ]]; then
    cp -a -- "$user_unit_dir/$dropin_dir" "$backup_dir/"
  fi
done
new_unit=""
cleanup_new_unit() {
  if [[ -n "$new_unit" && ( -e "$new_unit" || -L "$new_unit" ) ]]; then
    rm -f -- "$new_unit"
  fi
}
trap cleanup_new_unit EXIT
for unit in \
  brain-mcp-http.service \
  brain-mcp-http-watchdog.service \
  brain-mcp-http-watchdog.timer; do
  if [[ -e "$user_unit_dir/$unit" || -L "$user_unit_dir/$unit" ]]; then
    cp -a -- "$user_unit_dir/$unit" "$backup_dir/"
  fi
  new_unit="$(mktemp "$user_unit_dir/.$unit.new.XXXXXX")"
  install -m 0644 "$render_dir/$unit" "$new_unit"
  cmp -s "$render_dir/$unit" "$new_unit"
  mv -f -- "$new_unit" "$user_unit_dir/$unit"
  new_unit=""
done
systemctl --user daemon-reload
systemd-analyze --user verify \
  "$user_unit_dir/brain-mcp-http.service" \
  "$user_unit_dir/brain-mcp-http-watchdog.service" \
  "$user_unit_dir/brain-mcp-http-watchdog.timer"
systemctl --user show brain-mcp-http.service \
  -p LoadState -p ActiveState -p SubState -p UnitFileState -p FragmentPath -p DropInPaths
systemctl --user show brain-mcp-http-watchdog.timer \
  -p LoadState -p ActiveState -p SubState -p UnitFileState -p FragmentPath -p DropInPaths
```

### Préflight de fichiers hors systemd

Ce préflight est exécutable depuis le shell opérateur : il atteste uniquement le checkout, le
flag partagé et le fichier privé. Il ne charge, ne source et n'affiche aucune valeur privée. Ne
pas lui ajouter `--require-effective-private` : cette option exige l'environnement finalement
construit par systemd, absent du shell opérateur.

```bash
set -euo pipefail
repo_root="$(pwd)"
"$repo_root/.venv/bin/python" "$repo_root/scripts/check_graph_projector_env.py" \
  --shared "$repo_root/.env" \
  --private "$HOME/.config/brain-v42/graph-projector.env"
```

Le preflight MCP atteste `.env` et `mcp-token.env` par `lstat` : fichiers réguliers sans
symlink, propriétaire identique au service, mode `0600`, taille bornée et exactement un
`MCP_HTTP_TOKEN` non vide. Au démarrage systemd, il compare aussi les valeurs effectives du
bearer administrateur, du registre Dream et de son flag d'activation sans jamais afficher un
secret. Le fichier privé est réservé à `MCP_HTTP_TOKEN`, `MCP_HTTP_DREAM_TOKENS` et
`BRAIN_DREAM_CAPABILITY_ENFORCEMENT` ; toute autre affectation, casse concurrente ou syntaxe
`export` est rejetée. Le preflight du projecteur accepte l'absence du fichier privé uniquement si
`GRAPH_LEDGER_WRITE_ENABLED` est effectivement fermé. Avec le ledger actif, il exige un fichier
régulier, non symlink, possédé par l'utilisateur du service, en mode `0600`, avec exactement les
quatre clés `GRAPH_PROJECTOR_*` attendues et un mot de passe non-placeholder.

Avant le canary, le processus client doit recevoir le même bearer via son gestionnaire de
secrets : `.mcp.json` développe `${MCP_HTTP_TOKEN}` dans l'en-tête `Authorization`. Ne pas
réintroduire ce secret dans `.env` et ne pas `source` le fichier systemd depuis Bash : les deux
grammaires diffèrent. Depuis l'environnement privé qui lancera ou relancera le client, vérifier
la concordance sans afficher la valeur :

```bash
set -euo pipefail
repo_root="$(pwd)"
test -n "${MCP_HTTP_TOKEN:-}"
"$repo_root/.venv/bin/python" "$repo_root/scripts/check_mcp_http_port.py" \
  --shared "$repo_root/.env" --expected 8765 --expected-host 127.0.0.1 \
  --token-file "$HOME/.config/brain-v42/mcp-token.env" \
  --require-effective-token
```

## First activation or host migration

Garder le watchdog désactivé pendant le canary : son rôle est de redémarrer le service sur un
échec de `/health`, ce qui masquerait un défaut de configuration répété.

```bash
set -euo pipefail
systemctl --user stop brain-mcp-http-watchdog.timer
systemctl --user stop brain-mcp-http-watchdog.service
systemctl --user disable --no-reload brain-mcp-http-watchdog.timer
```

### Attestation effective par systemd

Après `daemon-reload`, le redémarrage est l'attestation effective : systemd charge ses
`EnvironmentFile` puis exécute l'`ExecStartPre` qui conserve
`--require-effective-private`. Ne pas reproduire ce contrôle dans le shell et ne jamais
`source` le fichier privé ; un échec ici bloque le démarrage sans imprimer les secrets.

```bash
set -euo pipefail
old_pid="$(systemctl --user show brain-mcp-http.service -p MainPID --value || true)"
systemctl --user restart brain-mcp-http.service
systemctl --user is-active --quiet brain-mcp-http.service
new_pid="$(systemctl --user show brain-mcp-http.service -p MainPID --value)"
test "$new_pid" -gt 0
if [[ -n "$old_pid" && "$old_pid" != 0 ]]; then
  test "$new_pid" != "$old_pid"
fi
```

Poursuivre immédiatement le canary :

```bash
set -euo pipefail
healthy=false
for attempt in {1..30}; do
  if curl -fsS -m 2 http://127.0.0.1:8765/health; then
    healthy=true
    break
  fi
  sleep 1
done
if [[ "$healthy" != true ]]; then
  journalctl --user -u brain-mcp-http.service --since '-10 min' --no-pager
  exit 1
fi
journalctl --user -u brain-mcp-http.service --since '-10 min' --no-pager
```

Après le healthcheck, exécuter depuis un client de production un appel Brain **en lecture seule**
et vérifier son identité dans les métriques. Pour une migration d'hôte, conserver l'ancien hôte
disponible jusqu'à cette preuve et basculer les clients un par un. Ne pas activer le watchdog tant
que les appels et les journaux ne sont pas stables.

Le go/no-go opérateur rend ensuite le serveur et le watchdog persistants :

```bash
systemctl --user enable brain-mcp-http.service
systemctl --user enable --now brain-mcp-http-watchdog.timer
```

Le linger de l'utilisateur est un prérequis séparé pour survivre à une déconnexion. Le vérifier
avec `loginctl show-user "$(id -u)" -p Linger`; toute modification de linger requiert les droits
administrateur de l'hôte et n'est pas effectuée par l'installateur.

## Validation

```bash
set -euo pipefail
systemctl --user is-enabled brain-mcp-http.service
systemctl --user is-active brain-mcp-http.service
systemctl --user is-enabled brain-mcp-http-watchdog.timer
systemctl --user is-active brain-mcp-http-watchdog.timer
curl -fsS -m 10 http://127.0.0.1:8765/health
systemctl --user list-timers brain-mcp-http-watchdog.timer --no-pager
journalctl --user -u brain-mcp-http.service \
  -u brain-mcp-http-watchdog.service --since '-30 min' --no-pager
```

`/health` est exempté d'authentification et prouve la liveness du serveur ainsi qu'un checkout
PostgreSQL borné. Il ne remplace ni un appel MCP réel, ni les contrôles GPU/reranker, ni la preuve
de portée du client.

## Rollback

Toujours neutraliser le timer avant le serveur, puis conserver les fichiers privés pour un
diagnostic hors journal.

```bash
set -euo pipefail
systemctl --user stop brain-mcp-http-watchdog.timer
systemctl --user stop brain-mcp-http-watchdog.service
systemctl --user disable --no-reload brain-mcp-http-watchdog.timer
systemctl --user disable --now brain-mcp-http.service
```

Lors d'une régression de version, exécuter `--check-only` depuis un checkout connu bon, produire
un nouveau `--render-dir`, puis restaurer uniquement les trois basenames MCP depuis
`$backup_dir` créé au preflight, après neutralisation du watchdog. Une atomicité multi-fichiers
n'existe pas : la garantie fournie ci-dessous est compensatoire et fail-closed. Les trois
remplacements et les trois snapshots de l'état courant sont préparés avant toute mutation ; si un
remplacement échoue, les unités déjà remplacées sont restaurées en ordre inverse et la commande
échoue non-zéro. Si la compensation échoue aussi, elle laisse les snapshots dans le répertoire de
stage pour récupération manuelle et échoue non-zéro, sans continuer vers `daemon-reload`.

### Rollback compensatoire des unités

Cette commande s'exécute dans le même shell que le preflight. Elle échoue avant mutation si une
sauvegarde ou une unité courante manque. Elle ne touche ni aux `EnvironmentFile`, ni aux fichiers
privés, ni aux credentials Neo4j. Après un succès, recharger le manager puis reprendre le
préflight de fichiers, l'attestation systemd et le canary.

```bash
set -euo pipefail
rollback_units=(
  brain-mcp-http.service \
  brain-mcp-http-watchdog.service \
  brain-mcp-http-watchdog.timer
)
rollback_stage_dir="$(mktemp -d "$user_unit_dir/.brain-mcp-http-rollback.XXXXXX")"
chmod 0700 "$rollback_stage_dir"
rollback_committed=()

rollback_compensate() {
  local index unit compensation_unit
  for ((index=${#rollback_committed[@]} - 1; index>=0; index--)); do
    unit="${rollback_committed[index]}"
    compensation_unit="$(mktemp "$user_unit_dir/.$unit.compensate.XXXXXX")"
    install -m 0644 "$rollback_stage_dir/original.$unit" "$compensation_unit" || return 1
    cmp -s "$rollback_stage_dir/original.$unit" "$compensation_unit" || return 1
    mv -f -- "$compensation_unit" "$user_unit_dir/$unit" || return 1
  done
}

rollback_failed() {
  local status="$1"
  trap - ERR
  if ! rollback_compensate; then
    echo 'ERROR: rollback compensation failed; retain the stage directory for manual recovery' >&2
  fi
  exit "$status"
}

trap 'rollback_failed $?' ERR
for unit in "${rollback_units[@]}"; do
  test -f "$backup_dir/$unit"
  test -f "$user_unit_dir/$unit"
  install -m 0644 "$backup_dir/$unit" "$rollback_stage_dir/replacement.$unit"
  cmp -s "$backup_dir/$unit" "$rollback_stage_dir/replacement.$unit"
  install -m 0644 "$user_unit_dir/$unit" "$rollback_stage_dir/original.$unit"
  cmp -s "$user_unit_dir/$unit" "$rollback_stage_dir/original.$unit"
done
for unit in "${rollback_units[@]}"; do
  mv -f -- "$rollback_stage_dir/replacement.$unit" "$user_unit_dir/$unit"
  rollback_committed+=("$unit")
done
systemctl --user daemon-reload
trap - ERR
rm -rf -- "$rollback_stage_dir"
```

Sur une migration, repointer les clients vers l'ancien hôte seulement s'il est encore validé.

## Full uninstall

Cette commande n'est appropriée que si l'opérateur veut retirer toute la pile systemd gérée par
ce script. Elle arrête d'abord les timers/watchdogs, désactive les services, supprime leurs unités
et recharge le manager :

```bash
deploy/systemd/install.sh --uninstall
```

Après exécution, les fichiers de configuration et secrets ne sont pas supprimés. Les unités
gérées, en revanche, doivent toutes être absentes de
`${XDG_CONFIG_HOME:-$HOME/.config}/systemd/user`.
