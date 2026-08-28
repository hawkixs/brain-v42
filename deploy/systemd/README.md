# Runtime automation systemd — runbook opérateur

Ce dépôt livre `brain-v42-automation.service` **dormante**. L'installateur génère et
vérifie l'unité, mais ne l'active et ne la démarre jamais. Les commandes ci-dessous sont
destinées à un opérateur sur l'hôte cible; leur présence dans le dépôt ne signifie pas
qu'un cutover a eu lieu.

La topologie cible sépare les responsabilités:

- `brain-metrics.service` sur `127.0.0.1:9200` garde `/metrics` et `/api/cockpit`;
- `brain-v42-automation.service` sur `127.0.0.1:9201` porte `/health`, le webhook GitLab
  et la boucle de déduplication;
- une lease advisory PostgreSQL garantit au plus un propriétaire automation. Cette lease
  n'est pas un fencing token: un travail déjà entré en base au moment d'une coupure réseau
  ne peut pas être interrompu rétroactivement;
- le hook GitLab externe reste une décision séparée. Aucun bloc ne le repointe.

`GET :9201/health` prouve uniquement la **liveness** du processus HTTP. Ce signal n'est
pas une readiness PostgreSQL, GPU, reranker ou scheduler. Après la séparation, les
événements automation ne sont plus dans l'agrégat in-process `cockpit.recent`; leur trace
opérationnelle autoritaire passe dans le journal de l'unité automation.

Exécuter chaque section depuis la racine du dépôt. Ne passer à la suivante que si la
précédente sort avec le code `0`.

## Modes de rendu

- `install.sh --check-only` rend et vérifie toutes les unités gérées (la liste vit dans `MANAGED_UNIT_FILES`) dans un répertoire privé sous
  `/tmp`, puis le supprime. Il n'inspecte ni ne crée le répertoire systemd utilisateur et
  n'appelle pas `systemctl`.
- `install.sh --render-dir /chemin/absolu/neuf` produit les mêmes fichiers vérifiés dans
  une nouvelle cible privée hors de systemd. Le parent doit appartenir à l'utilisateur, avoir
  `u+wx`, ne pas être inscriptible par groupe/autres et ne contenir aucun composant symlinké.
- `install.sh --dry-run` est un mode historique : il n'appelle pas `systemctl`, mais **publie les
  unités gérées dans le répertoire systemd utilisateur**. Ne pas l'utiliser comme préflight sans
  effet de bord ni comme rollout global. Les chemins live `install` et `--dry-run` imposent un
  umask `077`, ramènent le répertoire final à `0700`, publient les unités en `0600` et refusent
  un propriétaire ou un ancêtre permettant le remplacement par un autre UID.

Les deux modes isolés exécutent les preflights avec le HOME/XDG hôte, puis un seul
`systemd-analyze verify` sur tous les artefacts rendus dans un HOME/XDG vide et privé. Ils échouent
fermés si le verifier manque ou refuse une unité.

En cas d'échec après publication `--render-dir`, le cleanup remet d'abord la cible dans son
staging privé et ne la supprime que si l'identité du rendu correspond encore. Un remplacement
concurrent est restauré ou laissé dans un staging signalé pour récupération. Cette défense vise
les erreurs et les autres UID ; un processus hostile partageant le même UID reste dans la même
frontière de confiance et nécessiterait un helper `dirfd` dédié.

## Preflight

Ce bloc vérifie d'abord toutes les unités gérées sans publication, produit un artefact inspectable hors
de systemd, sauvegarde le fragment automation et ses drop-ins, puis publie **uniquement** le
fragment automation par renommage atomique. Il recharge ensuite le manager avant inspection et
installe une sonde bornée de lease. La sonde ne montre aucune variable sensible : elle affiche
uniquement `owners` et `waiters`.

La sauvegarde et la publication du fragment sont des mutations opérateur. Ne pas exécuter ce
bloc sans fenêtre autorisée et rollback connu bon. Les unités Dream, graph et MCP rendues dans
l'artefact ne sont pas publiées par ce runbook.

### Render path terminology

`RENDER_PARENT` is the private, pre-existing parent directory that contains the rendered
artifacts and any backup directory. `RENDER_DIR` is the new child directory created inside it
for generated unit files; it is not the parent and must not exist before rendering. For example,
`/state/systemd-render.ABC123` is `RENDER_PARENT` and `/state/systemd-render.ABC123/units` is
`RENDER_DIR`. The installer validates the parent ancestry before creating the child.

<!-- runbook:preflight:start -->
```bash
set -euo pipefail
REPO_ROOT="${REPO_ROOT:-$(pwd)}"
cd "$REPO_ROOT"
USER_UNIT_DIR="${XDG_CONFIG_HOME:-$HOME/.config}/systemd/user"
EVIDENCE_DIR="${XDG_STATE_HOME:-$HOME/.local/state}/brain-v42-cutover"
LEASE_PROBE="${LEASE_PROBE:-$EVIDENCE_DIR/lease-probe.py}"
PROC_ROOT="${PROC_ROOT:-/proc}"
LOCK_KEY=4151019227643017711
mkdir -p "$EVIDENCE_DIR"
chmod 0700 "$EVIDENCE_DIR"
RENDER_PARENT="${RENDER_PARENT:-$(mktemp -d "$EVIDENCE_DIR/systemd-render.XXXXXX")}"
RENDER_DIR="${RENDER_DIR:-$RENDER_PARENT/units}"
BACKUP_DIR="${BACKUP_DIR:-$RENDER_PARENT/live-backup}"
readonly REPO_ROOT USER_UNIT_DIR EVIDENCE_DIR LEASE_PROBE RENDER_PARENT RENDER_DIR \
  BACKUP_DIR PROC_ROOT LOCK_KEY
mkdir -p "$RENDER_PARENT" "$BACKUP_DIR"
chmod 0700 "$RENDER_PARENT" "$BACKUP_DIR"
test ! -e "$RENDER_DIR" && test ! -L "$RENDER_DIR"

NEW_UNIT=""
cleanup_new_unit() {
  if [[ -n "$NEW_UNIT" && ( -e "$NEW_UNIT" || -L "$NEW_UNIT" ) ]]; then
    rm -f -- "$NEW_UNIT"
  fi
}
trap cleanup_new_unit EXIT

mkdir -p "$(dirname "$LEASE_PROBE")"
{
  printf '#!%s/.venv/bin/python\n' "$REPO_ROOT"
  cat <<'PYTHON'
import asyncio
import os

from sqlalchemy import text
from sqlalchemy.ext.asyncio import create_async_engine

from brain_v42.config import Settings

SQL = """
SELECT
    count(*) FILTER (WHERE granted) AS owners,
    count(*) FILTER (WHERE NOT granted) AS waiters
FROM pg_locks
WHERE locktype = 'advisory'
  AND classid::bigint = 966484478::bigint
  AND objid::bigint = 2541386223::bigint
  AND objsubid = 1
  AND database = (SELECT oid FROM pg_database WHERE datname = current_database())
  AND mode = 'ExclusiveLock'
"""


async def main() -> None:
    expected = int(os.environ["EXPECTED_AUTOMATION_LEASES"])
    engine = create_async_engine(Settings().postgres_url)
    try:
        async with engine.connect() as connection:
            row = (await connection.execute(text(SQL))).one()
    finally:
        await engine.dispose()
    owners = int(row.owners)
    waiters = int(row.waiters)
    print(f"owners={owners} waiters={waiters}")
    if owners != expected or waiters != 0:
        raise SystemExit(
            f"expected owners={expected} waiters=0, got owners={owners} waiters={waiters}"
        )


asyncio.run(main())
PYTHON
} > "$LEASE_PROBE"
chmod 0700 "$LEASE_PROBE"

"$REPO_ROOT/deploy/systemd/install.sh" --check-only
"$REPO_ROOT/deploy/systemd/install.sh" --render-dir "$RENDER_DIR"
test -f "$RENDER_DIR/brain-v42-automation.service"
if [[ -e "$USER_UNIT_DIR/brain-v42-automation.service" \
  || -L "$USER_UNIT_DIR/brain-v42-automation.service" ]]; then
  cp -a -- "$USER_UNIT_DIR/brain-v42-automation.service" "$BACKUP_DIR/"
fi
if [[ -d "$USER_UNIT_DIR/brain-v42-automation.service.d" ]]; then
  cp -a -- "$USER_UNIT_DIR/brain-v42-automation.service.d" "$BACKUP_DIR/"
fi
mkdir -p "$USER_UNIT_DIR"
NEW_UNIT="$(mktemp "$USER_UNIT_DIR/.brain-v42-automation.service.new.XXXXXX")"
install -m 0644 "$RENDER_DIR/brain-v42-automation.service" "$NEW_UNIT"
cmp -s "$RENDER_DIR/brain-v42-automation.service" "$NEW_UNIT"
mv -f -- "$NEW_UNIT" "$USER_UNIT_DIR/brain-v42-automation.service"
NEW_UNIT=""
systemctl --user daemon-reload
systemctl --user show-environment >/dev/null
test -f "$USER_UNIT_DIR/brain-v42-automation.service"
systemd-analyze --user verify "$USER_UNIT_DIR/brain-v42-automation.service"
systemctl --user show brain-metrics.service -p EnvironmentFiles --value
systemctl --user show brain-metrics.service \
  -p ActiveState -p SubState -p MainPID -p UnitFileState \
  -p FragmentPath -p DropInPaths \
  -p Requires -p Wants -p PartOf -p BindsTo -p Conflicts
systemctl --user show brain-v42-automation.service \
  -p ActiveState -p SubState -p MainPID -p UnitFileState \
  -p FragmentPath -p DropInPaths \
  -p Requires -p Wants -p PartOf -p BindsTo -p Conflicts
MAIN_PID="$(systemctl --user show brain-metrics.service -p MainPID --value)"
[[ "$MAIN_PID" =~ ^[1-9][0-9]*$ ]] || {
  printf 'ERROR: brain-metrics has no running MainPID\n' >&2
  exit 1
}
effective_legacy_flag="$(tr '\0' '\n' < "$PROC_ROOT/$MAIN_PID/environ" \
  | grep -E '^METRICS_LEGACY_AUTOMATION_ENABLED=' || true)"
printf '%s\n' "$effective_legacy_flag"
[[ "$effective_legacy_flag" == 'METRICS_LEGACY_AUTOMATION_ENABLED=true' ]] || {
  printf 'ERROR: expected METRICS_LEGACY_AUTOMATION_ENABLED=true before cutover\n' >&2
  exit 1
}
# Expected before cutover: owners=1 waiters=0.
EXPECTED_AUTOMATION_LEASES=1 "$LEASE_PROBE"
command -v ss >/dev/null || {
  printf 'ERROR: ss is required to verify TCP port 9201\n' >&2
  exit 1
}
tcp_9201_listeners="$(ss -H -ltn 'sport = :9201' 2>&1)"
if [[ -n "$tcp_9201_listeners" ]]; then
  printf 'ERROR: TCP port 9201 is already bound or could not be inspected\n' >&2
  exit 1
fi
preflight_9201="$({
  curl --silent --show-error --output /dev/null --write-out '%{http_code}' \
    --connect-timeout 2 --max-time 5 http://127.0.0.1:9201/health
} || true)"
printf 'preflight automation health status=%s\n' "${preflight_9201:-000}"
case "${preflight_9201:-000}" in
  000) ;;
  *) printf 'ERROR: automation port 9201 is already bound (HTTP %s)\n' \
       "$preflight_9201" >&2; exit 1 ;;
esac
```
<!-- runbook:preflight:end -->

Le résultat attendu avant cutover est le flag effectif legacy `true`, lu sans afficher les
autres variables du processus, puis une lease `owners=1 waiters=0` détenue par metrics.
Le port TCP `9201` doit être libre même si aucun serveur HTTP ne répond. Tout autre résultat
interdit de continuer.

## Cutover

Le drop-in porte une seconde `EnvironmentFile=` chargée après le `.env` de l'unité metrics.
Le fichier dédié est privé (`0600`) et devient la source autoritaire du flag. On arrête
metrics avant de démarrer automation: aucun dual-run n'est toléré.

<!-- runbook:cutover:start -->
```bash
set -euo pipefail
REPO_ROOT="${REPO_ROOT:-$(pwd)}"
cd "$REPO_ROOT"
USER_UNIT_DIR="${XDG_CONFIG_HOME:-$HOME/.config}/systemd/user"
OWNER_ENV="$HOME/.config/brain-v42/automation-owner.env"
DROPIN_DIR="$USER_UNIT_DIR/brain-metrics.service.d"
DROPIN="$DROPIN_DIR/90-automation-owner.conf"
LEASE_PROBE="${LEASE_PROBE:-${XDG_STATE_HOME:-$HOME/.local/state}/brain-v42-cutover/lease-probe.py}"
PROC_ROOT="${PROC_ROOT:-/proc}"

assert_http_status() {
  local expected="$1" method="$2" url="$3" actual
  actual="$(curl --silent --show-error --output /dev/null --write-out '%{http_code}' \
    --connect-timeout 2 --max-time 5 -X "$method" "$url")"
  [[ "$actual" == "$expected" ]] || {
    printf 'HTTP %s %s: expected %s, got %s\n' "$method" "$url" "$expected" "$actual" >&2
    return 1
  }
}

assert_process_flag() {
  local unit="$1" expected="$2" effective
  MAIN_PID="$(systemctl --user show "$unit" -p MainPID --value)"
  [[ "$MAIN_PID" =~ ^[1-9][0-9]*$ ]]
  effective="$(tr '\0' '\n' < "$PROC_ROOT/$MAIN_PID/environ" \
    | grep -E '^METRICS_LEGACY_AUTOMATION_ENABLED=' || true)"
  [[ "$effective" == "METRICS_LEGACY_AUTOMATION_ENABLED=$expected" ]]
}

umask 077
mkdir -p "$(dirname "$OWNER_ENV")" "$DROPIN_DIR"
printf '%s\n' 'METRICS_LEGACY_AUTOMATION_ENABLED=false' > "$OWNER_ENV"
chmod 0600 "$OWNER_ENV"
cat > "$DROPIN" <<'SYSTEMD'
[Service]
EnvironmentFile=%h/.config/brain-v42/automation-owner.env
SYSTEMD
chmod 0644 "$DROPIN"
systemctl --user daemon-reload

environment_files="$(
  systemctl --user show brain-metrics.service -p EnvironmentFiles --value
)"
case "$environment_files" in
  *"$OWNER_ENV (ignore_errors=no)") ;;
  *) printf 'late EnvironmentFile is not last: %s\n' "$environment_files" >&2; exit 1 ;;
esac

systemctl --user stop brain-metrics.service
EXPECTED_AUTOMATION_LEASES=0 "$LEASE_PROBE"
systemctl --user start brain-v42-automation.service
assert_http_status 200 GET http://127.0.0.1:9201/health
EXPECTED_AUTOMATION_LEASES=1 "$LEASE_PROBE"
systemctl --user start brain-metrics.service
assert_process_flag brain-metrics.service false
assert_http_status 200 GET http://127.0.0.1:9200/metrics
assert_http_status 404 POST http://127.0.0.1:9200/gitlab/webhook
EXPECTED_AUTOMATION_LEASES=1 "$LEASE_PROBE"
```
<!-- runbook:cutover:end -->

La preuve autoritaire du flag est la lecture filtrée de
`/proc/$MainPID/environ` après démarrage. `systemctl show -p Environment` ne suffit pas:
il peut afficher une configuration déclarée sans prouver l'environnement du processus.

## Abort immédiat

Exécuter ce bloc au premier échec du cutover. La vérification `owners=0 waiters=0` précède
strictement l'écriture du flag `true` et le redémarrage metrics. Avec `set -e`, une lease
encore détenue arrête le bloc avant ces mutations.

<!-- runbook:abort:start -->
```bash
set -euo pipefail
REPO_ROOT="${REPO_ROOT:-$(pwd)}"
cd "$REPO_ROOT"
OWNER_ENV="$HOME/.config/brain-v42/automation-owner.env"
LEASE_PROBE="${LEASE_PROBE:-${XDG_STATE_HOME:-$HOME/.local/state}/brain-v42-cutover/lease-probe.py}"
PROC_ROOT="${PROC_ROOT:-/proc}"

assert_http_status() {
  local expected="$1" method="$2" url="$3" actual
  actual="$(curl --silent --show-error --output /dev/null --write-out '%{http_code}' \
    --connect-timeout 2 --max-time 5 -X "$method" "$url")"
  [[ "$actual" == "$expected" ]]
}

assert_process_flag() {
  local unit="$1" expected="$2" effective
  MAIN_PID="$(systemctl --user show "$unit" -p MainPID --value)"
  [[ "$MAIN_PID" =~ ^[1-9][0-9]*$ ]]
  effective="$(tr '\0' '\n' < "$PROC_ROOT/$MAIN_PID/environ" \
    | grep -E '^METRICS_LEGACY_AUTOMATION_ENABLED=' || true)"
  [[ "$effective" == "METRICS_LEGACY_AUTOMATION_ENABLED=$expected" ]]
}

systemctl --user stop brain-v42-automation.service
systemctl --user reset-failed brain-v42-automation.service
EXPECTED_AUTOMATION_LEASES=0 "$LEASE_PROBE"
umask 077
mkdir -p "$(dirname "$OWNER_ENV")"
printf '%s\n' 'METRICS_LEGACY_AUTOMATION_ENABLED=true' > "$OWNER_ENV"
chmod 0600 "$OWNER_ENV"
systemctl --user daemon-reload
systemctl --user restart brain-metrics.service
assert_process_flag brain-metrics.service true
assert_http_status 200 GET http://127.0.0.1:9200/metrics
assert_http_status 401 POST http://127.0.0.1:9200/gitlab/webhook
EXPECTED_AUTOMATION_LEASES=1 "$LEASE_PROBE"
```
<!-- runbook:abort:end -->

## Smoke tests

Ce bloc est une matrice fail-fast. Il prouve la surface réduite de `:9201`, la surface
metrics intacte sur `:9200`, la disparition du webhook legacy et l'unicité de la lease.

<!-- runbook:smoke:start -->
```bash
set -euo pipefail
REPO_ROOT="${REPO_ROOT:-$(pwd)}"
cd "$REPO_ROOT"
LEASE_PROBE="${LEASE_PROBE:-${XDG_STATE_HOME:-$HOME/.local/state}/brain-v42-cutover/lease-probe.py}"

assert_http_status() {
  local expected="$1" method="$2" url="$3" actual
  actual="$(curl --silent --show-error --output /dev/null --write-out '%{http_code}' \
    --connect-timeout 2 --max-time 5 -X "$method" "$url")"
  [[ "$actual" == "$expected" ]] || {
    printf 'HTTP %s %s: expected %s, got %s\n' "$method" "$url" "$expected" "$actual" >&2
    return 1
  }
}

assert_http_status 200 GET http://127.0.0.1:9201/health
assert_http_status 404 GET http://127.0.0.1:9201/metrics
assert_http_status 404 GET http://127.0.0.1:9201/api/cockpit
assert_http_status 401 POST http://127.0.0.1:9201/gitlab/webhook
assert_http_status 200 GET http://127.0.0.1:9200/metrics
assert_http_status 200 GET http://127.0.0.1:9200/api/cockpit
assert_http_status 404 POST http://127.0.0.1:9200/gitlab/webhook
EXPECTED_AUTOMATION_LEASES=1 "$LEASE_PROBE"
```
<!-- runbook:smoke:end -->

Ne repointer le hook GitLab vers `:9201` qu'après une décision opérateur séparée. Ne faire
`systemctl --user enable brain-v42-automation.service` qu'après le soak convenu.

## Diagnostics

Un bind rouge se diagnostique sans démarrer une seconde instance. Un **lease conflict** au
démarrage signifie qu'un propriétaire est encore actif: ne jamais forcer ni contourner la
lease. Un webhook authentifié qui répond `503` avec `ownership_lost` confirme la perte de
propriété fail-closed; consulter le journal avant toute décision de redémarrage.

```bash
set -euo pipefail
REPO_ROOT="${REPO_ROOT:-$(pwd)}"
cd "$REPO_ROOT"
LEASE_PROBE="${LEASE_PROBE:-${XDG_STATE_HOME:-$HOME/.local/state}/brain-v42-cutover/lease-probe.py}"
ss -ltnp 'sport = :9201'
systemctl --user status brain-v42-automation.service --no-pager
journalctl --user -u brain-v42-automation.service --since '-30 min' --no-pager
curl --silent --show-error --connect-timeout 2 --max-time 5 \
  http://127.0.0.1:9201/health
EXPECTED_AUTOMATION_LEASES=1 "$LEASE_PROBE"
```

La sortie du journal remplace la visibilité in-process perdue de `cockpit.recent` pour les
événements automation; `/metrics` et `/api/cockpit` restent servis par metrics sur `:9200`.

## Rollback

Prérequis externe obligatoire: désactiver le hook GitLab hors de ce dépôt, sans le
repointer. Exporter `HOOK_DISABLED_CONFIRMED=yes` seulement après confirmation. Le rollback
ne réactive le hook qu'après un vert complet et une nouvelle décision séparée.

<!-- runbook:rollback:start -->
```bash
set -euo pipefail
if [[ "${HOOK_DISABLED_CONFIRMED:-}" != "yes" ]]; then
  printf 'ERROR: HOOK_DISABLED_CONFIRMED must equal yes before rollback\n' >&2
  exit 1
fi
REPO_ROOT="${REPO_ROOT:-$(pwd)}"
cd "$REPO_ROOT"
OWNER_ENV="$HOME/.config/brain-v42/automation-owner.env"
LEASE_PROBE="${LEASE_PROBE:-${XDG_STATE_HOME:-$HOME/.local/state}/brain-v42-cutover/lease-probe.py}"
PROC_ROOT="${PROC_ROOT:-/proc}"

assert_http_status() {
  local expected="$1" method="$2" url="$3" actual
  actual="$(curl --silent --show-error --output /dev/null --write-out '%{http_code}' \
    --connect-timeout 2 --max-time 5 -X "$method" "$url")"
  [[ "$actual" == "$expected" ]]
}

assert_process_flag() {
  local unit="$1" expected="$2" effective
  MAIN_PID="$(systemctl --user show "$unit" -p MainPID --value)"
  [[ "$MAIN_PID" =~ ^[1-9][0-9]*$ ]]
  effective="$(tr '\0' '\n' < "$PROC_ROOT/$MAIN_PID/environ" \
    | grep -E '^METRICS_LEGACY_AUTOMATION_ENABLED=' || true)"
  [[ "$effective" == "METRICS_LEGACY_AUTOMATION_ENABLED=$expected" ]]
}

systemctl --user stop brain-v42-automation.service
systemctl --user disable brain-v42-automation.service
EXPECTED_AUTOMATION_LEASES=0 "$LEASE_PROBE"
umask 077
mkdir -p "$(dirname "$OWNER_ENV")"
printf '%s\n' 'METRICS_LEGACY_AUTOMATION_ENABLED=true' > "$OWNER_ENV"
chmod 0600 "$OWNER_ENV"
systemctl --user daemon-reload
systemctl --user restart brain-metrics.service
assert_process_flag brain-metrics.service true
assert_http_status 200 GET http://127.0.0.1:9200/metrics
assert_http_status 401 POST http://127.0.0.1:9200/gitlab/webhook
EXPECTED_AUTOMATION_LEASES=1 "$LEASE_PROBE"
```
<!-- runbook:rollback:end -->

Conserver le template, le drop-in et le fichier d'environnement pendant tout le soak. Leur
suppression est une opération de nettoyage ultérieure, jamais une étape du rollback urgent.
