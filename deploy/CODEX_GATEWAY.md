# Codex gateway — déploiement privé par Docker Compose

Ce runbook déploie la passerelle d'administration consommée par `red-codex`. Il décrit une
procédure opérateur; sa présence dans le dépôt ne prouve pas qu'un déploiement live a réussi.

> **Activation live interdite** tant que `codex_ro` et le compte propriétaire `brain`
> utilisent leurs credentials de développement par défaut. La rotation doit inclure une
> connexion neuve réussie avec le nouveau secret et le refus prouvé de l'ancien.

La passerelle appartient exclusivement au service Compose `brain-codex-gateway`. Elle écoute
sur le port logique `9211` dans `brain_v42_default`, sans port publié sur l'hôte. Cette valeur
applique la décision Brain `61574e9a-5ad3-457f-b0bb-58ec01f5e73a`; le port `9210` reste réservé
à `red-shrik`. Le consommateur utilise donc l'URL interne
`http://brain-codex-gateway:9211`.

Le service porte le profil Compose dormant `codex-gateway` : un `docker compose up -d` global
ne le démarre jamais. Seul un ciblage explicite de `brain-codex-gateway`, après validation des
gates de ce runbook, active ce profil.

N'installez, n'activez et ne démarrez aucune unité systemd pour cette passerelle.
`deploy/systemd/install.sh` ne la gère pas.

## Préconditions

Exécutez les commandes depuis la racine de `brain_v42`. Avant le déploiement :

- PostgreSQL doit être sain dans la stack Compose;
- la migration Alembic `036` doit être dans la chaîne appliquée; la production actuelle doit
  annoncer le head Alembic effectivement déployé, mesuré immédiatement avant la procédure — ne
  jamais le recopier d'une exécution précédente;
- les credentials PostgreSQL `codex_ro` et `brain` doivent être non défaut et vérifiés;
- le fichier de killswitch Dream doit exister sur l'hôte;
- l'API `red-codex` doit rejoindre le réseau externe `brain_v42_default`;
- Docker Compose doit pouvoir construire la cible `production`.

La migration `036` ajoute neuf vues et actualise `codex_brain_entity_v1`, soit dix vues
vérifiées par la readiness. Son `CREATE OR REPLACE` actualise la vue existante sans changer
son OID ni casser ses dépendants. Il ajoute `indexed_plans` à la découverte des sous-partitions
`red`, ce qui rend visibles les plans d'une sous-partition présente uniquement dans cette
table. Les familles tickets, features et proposals restent limitées au groupe `red`; les vues
Dream et consolidation restent globales par contrat.

Les sept vues qui filtrent le groupe `red` utilisent `security_barrier=true`. Cette barrière
empêche PostgreSQL de pousser une fonction fournie par `codex_ro` sous le filtre de
confidentialité et d'observer une ligne hors scope par effet de bord.

La même migration ajoute deux fences SQL :

- `trg_feature_artifact_live_target` refuse un nouvel artifact vers une feature archivée ou
  fusionnée;
- `trg_ticket_participants_immutable` interdit toute modification de `from_project` ou
  `to_project` après création du ticket.

```bash
# POSTGRES_URL est injecté par le gestionnaire de secrets, sans l'afficher.
BRAIN_ALEMBIC_ALLOW_PROD=1 uv run alembic current
```

La tête du dépôt avance à chaque migration mergée ; ne recopiez jamais son numéro d'une exécution
précédente. Sur cette production, `alembic current` doit annoncer le head déployé, mesuré avant la
procédure. La migration 037 descend de 036 et conserve les dix vues requises par la gateway. La
migration 038 ajoute le journal des tentatives Dream EXTRACT et la migration 039 isole le trigger
de timestamp de `project_contexts`; aucune migration postérieure à 036 ne fait partie du cutover
gateway. Ce runbook n'applique aucune migration Alembic au-delà de ce que la production porte déjà.

Arrêtez si la révision mesurée est antérieure à `036`; ne downgradez jamais vers 036 pour
satisfaire ce runbook. Sur un environnement neuf, exécutez d'abord le runbook de migration
principal avec son autorisation et ses preuves propres, puis revenez ici.

## Lever les blocages de credentials

`scripts/rotate_codex_gateway_credentials.py` est l'autorité de rotation pour les rôles
PostgreSQL `brain` et `codex_ro` ainsi que pour le bearer gateway. Il reprend le contrat du
rotateur Neo4j : dry-run par défaut, inventaire fermé, journal privé `0600`, verrou exclusif,
écritures atomiques, reprise et rollback. Il ne reçoit aucun secret par argument ou variable
d'environnement et ses résultats JSON ne contiennent que des preuves sanitisées.

Le coordinateur met à jour cinq consommateurs privés : `.env` de `brain_v42`, `.env` de
`red-data`, `.env.local` de `red-codex`, `/etc/shrik/env` et
`~/.config/brain-v42/codex-gateway.env`. Il durcit aussi le fichier `red-data` à `0600`.
Tous ces processus figent leur DSN ou bearer au démarrage : la rotation se déroule donc en deux
phases séparées par leur recréation explicite.

Installez une fois la frontière privilégiée depuis le checkout revu. C'est le seul geste qui
demande un sudo interactif :

```bash
sudo ./deploy/install-brain-shrik-env-control.sh
```

L'installeur pose `/usr/local/sbin/brain-shrik-env-control` en `root:root 0755` et son drop-in
sudoers en `root:root 0440`, valide les grants avec `visudo`, puis exécute le `--check` read-only.
Il ne réécrit ni n'affiche le contenu de `/etc/shrik/env` et ne change pas l'état du service. Les
seules commandes NOPASSWD sont `--check`, `--publish`, `--stop`, `--start` et `--is-active`.
N'ajoutez aucun grant générique pour `true`, `tee`, `install` ou `systemctl` à ce workflow.

Préparez uniquement le parent privé, puis lancez le dry-run depuis la racine canonique de
`brain_v42`. La commande valide les fichiers, la cohérence des anciens credentials, une
connexion TCP neuve pour chaque rôle, le scope de `codex_ro`, la production exactement au head
que VOUS avez déclaré — mesuré immédiatement avant la procédure, jamais recopié — et le
privilège non interactif borné nécessaire à `red-shrik`. Elle ne génère ni n'écrit aucun
secret :

```bash
set -euo pipefail
export BRAIN_ROOT="$(pwd -P)"
export RED_ROOT="/home/hawixs/hawkixs_infra/git_repo/ReD_v1"
export ROTATION_DIR="$HOME/.config/brain-v42/codex-gateway-rotation"
export SHRIK_ENV="/etc/shrik/env"
install -d -m 0700 "$(dirname "$ROTATION_DIR")"

# Mesuré, jamais recopié : la garde comparait autrefois à une constante `037` et la
# procédure est devenue inexécutable dès la migration suivante.
export DEPLOYED_HEAD="$(docker exec brain_v42_postgres \
  psql -U brain -d brain -Atc "select version_num from alembic_version;")"

uv run python scripts/rotate_codex_gateway_credentials.py \
  --brain-root "$BRAIN_ROOT" \
  --red-root "$RED_ROOT" \
  --private-dir "$ROTATION_DIR" \
  --shrik-env "$SHRIK_ENV" \
  --expected-alembic-revision "$DEPLOYED_HEAD"
```

Le dry-run doit terminer avec `status=preflight_ok`, `alembic_revision` égal au head que vous
avez déclaré, `old_credentials_valid=true` et `codex_scope_bounded=true`. Arrêtez au premier
écart. Le CLI n'applique jamais Alembic et n'accepte plus AUCUNE révision implicitement :
`--expected-alembic-revision` est requis et refuse une valeur vide ou malformée.

Capturez l'état actif des unités et conteneurs sans afficher leur environnement. Neutralisez
ensuite Dream, ses jobs auxiliaires et tous les consommateurs directs avant la fenêtre : MCP,
metrics, automation, les deux services Dagster de `red-data`, `red-shrik`, l'API `red-codex` et
une éventuelle gateway. Vérifiez qu'aucun de ces processus n'est encore actif avant d'attester
la quiescence au CLI.

```bash
systemctl --user stop \
  brain-v42-dream.timer brain-v42-graph-recon.timer \
  brain-v42-embedding-backfill.timer brain-mcp-http-watchdog.timer
systemctl --user stop \
  brain-v42-dream.service brain-v42-graph-recon.service \
  brain-v42-embedding-backfill.service brain-v42-automation.service \
  brain-metrics.service brain-mcp-http-watchdog.service brain-mcp-http.service
docker compose stop brain-codex-gateway
docker compose -f "$RED_ROOT/projects/red-data/docker-compose.yml" \
  --env-file "$RED_ROOT/projects/red-data/.env" \
  stop dagster-webserver dagster-daemon
sudo -n /usr/local/sbin/brain-shrik-env-control --stop
if sudo -n /usr/local/sbin/brain-shrik-env-control --is-active; then
  echo "ERROR: red-shrik.service est encore actif" >&2
  exit 1
else
  status=$?
  [[ "$status" -eq 3 ]] || exit "$status"
fi
docker compose -f "$RED_ROOT/projects/red-codex/docker-compose.local.yml" \
  --env-file "$RED_ROOT/projects/red-codex/.env.local" stop api
```

N'utilisez `--rollback-preflight-confirmed` qu'après avoir vérifié que ces mêmes commandes
peuvent restaurer l'état capturé. La première phase crée une génération neuve, tourne les deux
rôles dans une transaction PostgreSQL, installe les cinq fichiers et prouve les nouveaux mots
de passe acceptés et les anciens refusés. Elle doit s'arrêter à
`status=awaiting_consumer_recreation`; le journal reste alors volontairement présent :

```bash
uv run python scripts/rotate_codex_gateway_credentials.py \
  --brain-root "$BRAIN_ROOT" \
  --red-root "$RED_ROOT" \
  --private-dir "$ROTATION_DIR" \
  --shrik-env "$SHRIK_ENV" \
  --expected-alembic-revision "$DEPLOYED_HEAD" \
  --apply \
  --consumers-stopped-confirmed \
  --rollback-preflight-confirmed
```

Recréez d'abord les consommateurs de lecture et le MCP, puis la gateway privée sur `:9211` et
enfin `red-codex`. Réactivez les timers Dream uniquement après toutes les probes :

```bash
systemctl --user start \
  brain-mcp-http.service brain-mcp-http-watchdog.timer \
  brain-metrics.service brain-v42-automation.service
docker compose -f "$RED_ROOT/projects/red-data/docker-compose.yml" \
  --env-file "$RED_ROOT/projects/red-data/.env" \
  up -d --no-deps --force-recreate dagster-webserver dagster-daemon
sudo -n /usr/local/sbin/brain-shrik-env-control --start
sudo -n /usr/local/sbin/brain-shrik-env-control --is-active
export BRAIN_CODEX_GATEWAY_UID="$(id -u)"
export BRAIN_CODEX_GATEWAY_GID="$(id -g)"
docker compose up -d --no-deps --force-recreate brain-codex-gateway
docker compose -f "$RED_ROOT/projects/red-codex/docker-compose.local.yml" \
  --env-file "$RED_ROOT/projects/red-codex/.env.local" \
  up -d --no-deps --force-recreate api
```

La seconde phase relit exactement la génération journalisée. Elle prouve encore les nouveaux
credentials PostgreSQL acceptés, les anciens refusés, puis exécute depuis la gateway les trois
sondes bearer : absence refusée, ancien refusé, nouveau accepté. Elle supprime le journal
uniquement après ces preuves :

```bash
uv run python scripts/rotate_codex_gateway_credentials.py \
  --brain-root "$BRAIN_ROOT" \
  --red-root "$RED_ROOT" \
  --private-dir "$ROTATION_DIR" \
  --shrik-env "$SHRIK_ENV" \
  --expected-alembic-revision "$DEPLOYED_HEAD" \
  --apply --resume \
  --consumers-stopped-confirmed \
  --rollback-preflight-confirmed \
  --consumers-recreated-confirmed

systemctl --user start \
  brain-v42-graph-recon.timer brain-v42-embedding-backfill.timer brain-v42-dream.timer
```

Si la première phase échoue, elle tente automatiquement le rollback PostgreSQL et fichiers,
conserve le journal et demande `--resume`. Si une erreur survient après recréation, quiescez de
nouveau les consommateurs puis restaurez explicitement la génération précédente :

```bash
uv run python scripts/rotate_codex_gateway_credentials.py \
  --brain-root "$BRAIN_ROOT" \
  --red-root "$RED_ROOT" \
  --private-dir "$ROTATION_DIR" \
  --shrik-env "$SHRIK_ENV" \
  --expected-alembic-revision "$DEPLOYED_HEAD" \
  --rollback \
  --consumers-stopped-confirmed \
  --rollback-preflight-confirmed
```

Recréez ensuite uniquement les consommateurs qui étaient actifs dans l'état capturé. Ne
supprimez, n'éditez et ne copiez jamais le journal à la main. Il contient le matériel privé
strictement nécessaire à la reprise et au rollback.

Un rôle d'écriture dédié à la gateway reste un chantier ouvert. Les deux fonctions de trigger
s'exécutent avec les droits de l'appelant (`SECURITY INVOKER`, valeur PostgreSQL par défaut),
et les mutations ne disposent pas encore d'un contrat RLS complet. Un rôle étroit échouerait
sur les lectures des triggers; lui ajouter des droits larges annulerait le least privilege.
N'inventez ni rôle, ni grants compensatoires dans ce déploiement.

Ne faites ni `cat`, ni `echo` du fichier privé. Ne le copiez jamais dans le dépôt, un log ou
une commande enregistrée dans l'historique. `deploy/codex-gateway.env.example` documente
uniquement le nom de variable; son placeholder fait volontairement échouer le démarrage.
Le launcher exige un fichier régulier appartenant à son UID, le mode exact `0600`, une seule
clé `BRAIN_CODEX_GATEWAY_TOKEN` et un bearer d'au moins 32 octets.

Compose monte ce fichier en lecture seule dans `/run/secrets/codex-gateway.env`; il ne le
charge pas avec `env_file`. Il monte aussi le drop-in killswitch en lecture seule dans
`/run/brain-v42/killswitches.conf`. Vérifiez la source killswitch avant le démarrage :

```bash
export BRAIN_DREAM_KILLSWITCHES_FILE="${BRAIN_DREAM_KILLSWITCHES_FILE:-$HOME/.config/systemd/user/brain-v42-dream.service.d/killswitches.conf}"
test -f "$BRAIN_DREAM_KILLSWITCHES_FILE"
test ! -L "$BRAIN_DREAM_KILLSWITCHES_FILE"
```

Un bind-mount de fichier conserve l'inode monté. Après un remplacement atomique du drop-in
killswitch sur l'hôte, forcez la recréation de la gateway, puis rejouez les sondes `/ready` et
`/api/killswitches`; un simple restart du processus ne prouve pas que le nouvel inode est lu.

```bash
export BRAIN_CODEX_GATEWAY_UID="$(id -u)"
export BRAIN_CODEX_GATEWAY_GID="$(id -g)"
docker compose up -d --no-deps --force-recreate brain-codex-gateway
```

Vérifiez que les booléens renvoyés par `/api/killswitches` correspondent au nouveau fichier.
La même règle de recréation s'applique après un remplacement atomique du fichier de bearer.

## Construire et démarrer

Le launcher compare le propriétaire du secret à son UID courant. Exportez l'UID et le GID de
l'opérateur qui possède le fichier avant chaque création du conteneur. Les valeurs par défaut
`1001:1001` correspondent à l'hôte actuel; ces exports rendent le déploiement portable :

```bash
export BRAIN_CODEX_GATEWAY_UID="$(id -u)"
export BRAIN_CODEX_GATEWAY_GID="$(id -g)"
docker compose config --quiet
docker compose build brain-codex-gateway
docker compose up -d brain-codex-gateway
docker compose ps brain-codex-gateway
```

Attendez l'état `healthy`. Le healthcheck Compose appelle `/ready`, pas `/health`. `/health`
prouve seulement que le processus HTTP répond; il peut rester vert si PostgreSQL ou le contrat
SQL est indisponible. `/ready` exige une connexion PostgreSQL, les dix vues Codex, les deux
triggers de migration `036` actifs pour les écritures ordinaires (`ENABLE` ou `ENABLE ALWAYS`)
et `security_barrier=true` sur les sept vues scopées. Inspectez
uniquement l'état et les événements; n'affichez pas l'environnement du conteneur.

```bash
test "$(docker inspect --format '{{.State.Health.Status}}' brain_v42_codex_gateway)" = healthy
test "$(docker inspect --format '{{range .Mounts}}{{if eq .Destination "/run/secrets/codex-gateway.env"}}{{.RW}}{{end}}{{end}}' brain_v42_codex_gateway)" = false
test "$(docker inspect --format '{{range .Mounts}}{{if eq .Destination "/run/brain-v42/killswitches.conf"}}{{.RW}}{{end}}{{end}}' brain_v42_codex_gateway)" = false
```

L'écoute `0.0.0.0:9211` reste confinée au namespace réseau du conteneur : le service Compose
ne déclare ni `ports`, ni `expose`. N'ajoutez pas de publication hôte ou LAN pour faciliter un
test; effectuez les sondes depuis le réseau Docker.

## Configurer `red-codex`

Dans le fichier privé `.env.local` de `red-codex`, configurez :

```dotenv
CODEX_BRAIN_DSN=postgresql+asyncpg://codex_ro:<secret-roté>@brain_v42_postgres:5432/brain
CODEX_BRAIN_GATEWAY_URL=http://brain-codex-gateway:9211
CODEX_BRAIN_GATEWAY_TOKEN=<même valeur que BRAIN_CODEX_GATEWAY_TOKEN>
```

Gardez `.env.local` en mode `0600`, hors Git. Transférez le bearer par un gestionnaire de
secrets ou une saisie masquée; ne l'imprimez pas pour le copier. Le service `api` de
`red-codex` doit rester attaché à `brain_v42_default`, puis être recréé pour charger les deux
variables :

```bash
cd /chemin/vers/ReD_v1/projects/red-codex
docker compose -f docker-compose.local.yml --env-file .env.local \
  up -d --no-deps --force-recreate api
```

Avec les deux variables vides, `red-codex` conserve les lectures et désactive les mutations.

## Vérifier le déploiement

### Santé et authentification

Cette sonde s'exécute dans la passerelle. Elle vérifie la liveness `/health`, la readiness
`/ready`, le refus sans bearer et l'accès authentifié sans écrire ni afficher le secret :

```bash
docker compose exec -T brain-codex-gateway python - <<'PY'
import json
import urllib.error
import urllib.request
from pathlib import Path

from brain_v42.codex_gateway.launcher import load_gateway_token_file


def status(request: urllib.request.Request) -> int:
    try:
        with urllib.request.urlopen(request, timeout=5) as response:
            response.read()
            return response.status
    except urllib.error.HTTPError as error:
        error.read()
        return error.code


base = "http://127.0.0.1:9211"
assert status(urllib.request.Request(f"{base}/health")) == 200
assert status(urllib.request.Request(f"{base}/ready")) == 200
assert status(urllib.request.Request(f"{base}/api/killswitches")) == 401
token = load_gateway_token_file(
    Path("/run/secrets/codex-gateway.env")
).get_secret_value()
authenticated = urllib.request.Request(
    f"{base}/api/killswitches",
    headers={"Authorization": f"Bearer {token}"},
)
with urllib.request.urlopen(authenticated, timeout=5) as response:
    assert response.status == 200
    killswitches = json.load(response)
expected_keys = {
    "promote_enabled",
    "promote_dry",
    "reorg_enabled",
    "reorg_dry",
    "extract_enabled",
    "extract_dry",
    "roadmap_enabled",
    "roadmap_dry",
}
assert set(killswitches) == expected_keys
assert all(isinstance(value, bool) for value in killswitches.values())
print("gateway health/readiness/auth: OK")
print("killswitches:", json.dumps(killswitches, sort_keys=True))
PY
```

### Périmètre SQL `red`

Cette sonde en lecture seule vérifie les deux vues racines du périmètre. Les vues messages,
artifacts et proposals héritent de ces racines. Les vues globales Dream et consolidation ne
font volontairement pas partie de ce contrôle.

```bash
docker compose exec -T brain-codex-gateway python - <<'PY'
import asyncio

from sqlalchemy import text
from sqlalchemy.ext.asyncio import create_async_engine

from brain_v42.config import Settings


SQL = """
SELECT
  (
    SELECT count(*)
    FROM codex_feature_v1 AS feature
    WHERE NOT EXISTS (
      SELECT 1
      FROM project_contexts AS project
      WHERE project.project_group = 'red'
        AND (
          project.project_key = feature.project_key
          OR project.project_key = split_part(feature.project_key, ':', 1)
        )
    )
  ) AS feature_violations,
  (
    SELECT count(*)
    FROM codex_ticket_v1 AS ticket
    WHERE NOT EXISTS (
      SELECT 1
      FROM project_contexts AS project
      WHERE project.project_group = 'red'
        AND (
          project.project_key = ticket.from_project
          OR project.project_key = split_part(ticket.from_project, ':', 1)
        )
    )
      AND NOT EXISTS (
        SELECT 1
        FROM project_contexts AS project
        WHERE project.project_group = 'red'
          AND (
            project.project_key = ticket.to_project
            OR project.project_key = split_part(ticket.to_project, ':', 1)
          )
      )
  ) AS ticket_violations,
  (
    SELECT count(*)
    FROM codex_brain_entity_v1 AS entity
    WHERE entity.type = 'plan'
      AND NOT EXISTS (
        SELECT 1
        FROM project_contexts AS project
        WHERE project.project_group = 'red'
          AND (
            project.project_key = entity.project_key
            OR project.project_key = split_part(entity.project_key, ':', 1)
          )
      )
  ) AS plan_violations
"""


async def main() -> None:
    engine = create_async_engine(Settings().postgres_url)
    try:
        async with engine.connect() as connection:
            row = (await connection.execute(text(SQL))).one()
    finally:
        await engine.dispose()
    result = (
        int(row.feature_violations),
        int(row.ticket_violations),
        int(row.plan_violations),
    )
    assert result == (0, 0, 0), f"red scope violations: {result}"
    print("gateway red SQL scope: OK")


asyncio.run(main())
PY
```

### Chemin consommateur `red-codex`

Depuis l'hôte `red-codex`, vérifiez le statut relayé puis une lecture authentifiée. Ces
requêtes n'exposent pas le bearer :

```bash
curl --fail --silent http://127.0.0.1:8091/api/brain/gateway/status \
  | python -c 'import json,sys; assert json.load(sys.stdin) == {"configured": True}'
curl --fail --silent --output /dev/null \
  http://127.0.0.1:8091/api/brain/dream/killswitches
```

Le statut relayé par `red-codex` sonde `/ready`; il ne devient donc positif que lorsque la
connexion, les vues, les barriers et les triggers sont compatibles. Le `/ready` direct et
l'état `healthy` du conteneur restent néanmoins les preuves opérateur de référence.

Une validation complète exige les trois groupes de sondes verts. N'exécutez une mutation
métier qu'avec un ticket ou une proposal explicitement prévu pour ce test.

## Contrats opératoires et limites connues

- Une proposal déjà appliquée ou rejetée renvoie `409`. Une source modifiée depuis la revue
  renvoie aussi `409 Proposal state changed; review required`. Rechargez son état et faites-la
  revoir; ne rejouez pas aveuglément la mutation.
- Les participants `from_project` et `to_project` d'un ticket sont immuables après création,
  y compris pour un accès SQL direct. Créez un nouveau ticket si le routage doit changer.
- Le guard de scope ticket maintient une transaction pendant l'appel au service canonique.
  Le pool par processus contient 20 connexions plus 10 d'overflow, soit un plafond de 30.
  Cette version supporte un seul consommateur `red-codex` et des bursts strictement inférieurs
  à 30; restez nettement sous ce plafond pour les mutations ticket. N'ajoutez ni réplica de
  gateway, ni charge concurrente soutenue sans revoir la transaction et le dimensionnement.
- Le rôle gateway dédié reste bloqué par les droits `SECURITY INVOKER` des triggers et
  l'absence de RLS complète. Le compte `brain` roté est donc une dette explicite de cette
  version, pas un modèle least-privilege à recopier.

## Rollback

Le rollback coupe d'abord le consommateur, puis la passerelle. Dans `.env.local` de
`red-codex`, remettez `CODEX_BRAIN_GATEWAY_URL` et `CODEX_BRAIN_GATEWAY_TOKEN` à vide, puis
recréez son service `api`. Les écrans Brain restent disponibles en lecture seule.

```bash
cd /chemin/vers/ReD_v1/projects/red-codex
docker compose -f docker-compose.local.yml --env-file .env.local \
  up -d --no-deps --force-recreate api

cd /chemin/vers/brain_v42
docker compose stop brain-codex-gateway
```

Conservez le head déployé — mesuré avant le rollback, jamais recopié d'une exécution précédente —
pendant ce rollback : ses vues 036 restent le contrat de lecture de `red-codex`.
Le rollback de la gateway n'autorise aucune migration Alembic. Un éventuel downgrade de schéma
relève des runbooks lifecycle et graph, avec une autorisation opérateur distincte; ne downgradez
jamais pour revenir simplement à l'état précédent de la gateway.

Si le bearer peut avoir fuité, générez-en un nouveau, mettez à jour les deux fichiers privés,
puis recréez les deux services. Ne réutilisez jamais le bearer suspect.
