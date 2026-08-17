# Récupération contrôlée d’EXTRACT Dream

## Portée

Ce runbook prépare la récupération des embeddings manquants et le canary Dream. Il ne doit pas être exécuté dans cette tâche.

## Propriétaire et cadence

Brain operations possède `brain-v42-embedding-backfill.service`. Le timer prévu s’exécute chaque jour à 04:30 avec un lot maximal de 100 entités, par groupes de 20. Le service n’est pas installé ni activé par ce changement.

## Cause et stratégie de récupération

La barrière de déduplication EXTRACT refuse tout learning ou decision actif dont
l’embedding est absent ou de norme inférieure ou égale à `1e-6` : un tel vecteur
n’est pas comparable en cosinus. Historiquement, le worker de backfill ne
sélectionnait et ne remplaçait que les valeurs `NULL`. Une valeur de norme nulle
restait donc définitivement dans le corpus actif et faisait échouer chaque EXTRACT.

Le backfill et ses métriques sélectionnent maintenant la même définition de
non-comparabilité que la barrière Dream, et son compare-and-set remplace aussi
un vecteur de norme nulle uniquement si `updated_at` est inchangé. La barrière
reste fail-closed : si la réparation ne produit pas un corpus comparable,
EXTRACT persiste une tentative `failed` avec une cause expurgée et ne crée ni
n’applique de proposition.

Dans une fenêtre opérateur, contrôler le backlog sur une base isolée puis le
résorber par lots bornés ; le second passage doit stocker zéro embedding. Le
CLI lit `POSTGRES_URL` (et non `BRAIN_V42_TEST_DB_URL`). La séquence gardée
ci-dessous est le seul chemin qui autorise `--execute` : elle lie le snapshot,
valide une restauration isolée, puis lance le worker.

## Snapshot et rollback des écritures de backfill

Le backfill remplace `embedding` et `updated_at` en place. Il ne possède ni
journal des anciennes valeurs ni identifiant de lot persistant ; les métriques
`embedding_backfill.*` sont agrégées. Sans snapshot antérieur, un rollback
granulaire par learning ou decision est donc impossible. Ne pas prétendre
reconstituer un ancien vecteur depuis le rapport du worker.

Avant toute exécution `--execute`, identifier explicitement le scope
(`project_key`, types, `limit`, date/heure) et réserver une base de
restauration vide. Les URI ne doivent jamais être affichées. La garde canonise
les identités avec `sqlalchemy.engine.make_url`, comme le résolveur Alembic :
elle refuse tous les paramètres d’URI, qui pourraient redéfinir l’hôte, le
port ou la base effectivement ouverte. Elle compare ensuite exactement
`hôte/port/nom_de_base` de `POSTGRES_URL` et `BACKFILL_PGURL`.

`BACKFILL_RESTORE_PGURL` reste explicite, mais la garde vérifie qu’il vise
exactement `BACKFILL_RESTORE_DB`, qu’il est distinct de la base opérée, et que
son hôte/port est le même que l’URI d’administration passée à `createdb`. Le
restore validé est donc un prérequis bloquant de `--execute`, non une étape
facultative.

<!-- backfill-recovery-guard:start -->
```bash
set -euo pipefail
: "${POSTGRES_URL:?URI asyncpg de la base opérée requise}"
: "${BACKFILL_PGURL:?URI libpq de la base à snapshotter requise}"
: "${BACKFILL_PROJECT:?project_key explicite requis}"
: "${BACKFILL_SNAPSHOT_DIR:?répertoire de snapshots explicite requis}"
: "${BACKFILL_RESTORE_DB:?nom de base isolée vide requis}"
: "${BACKFILL_RESTORE_ADMIN_PGURL:?URI libpq d’administration requise}"
: "${BACKFILL_RESTORE_PGURL:?URI libpq de restauration requise}"

# Ne journalise ni URI ni secret. Toute divergence sort avant pg_dump, pg_restore
# et le worker. make_url rejette aussi les paramètres qui pourraient modifier la cible.
"${BACKFILL_PYTHON:-python}" - \
  "$POSTGRES_URL" "$BACKFILL_PGURL" "$BACKFILL_RESTORE_ADMIN_PGURL" \
  "$BACKFILL_RESTORE_PGURL" "$BACKFILL_RESTORE_DB" <<'PY'
from sqlalchemy.engine import make_url
import sys


def identity(raw: str, label: str, *, asyncpg: bool = False) -> tuple[str, int, str]:
    try:
        url = make_url(raw)
    except Exception:
        raise SystemExit(f"{label} database identity is invalid") from None
    if url.query:
        raise SystemExit(f"{label} database identity must not include query parameters")
    expected_driver = "postgresql+asyncpg" if asyncpg else "postgresql"
    if url.drivername != expected_driver:
        raise SystemExit(f"{label} database identity has an invalid driver")
    if not url.host or url.port is None or not url.database:
        raise SystemExit(f"{label} database identity must include host, port, and database")
    return (url.host.casefold(), url.port, url.database)


target = identity(sys.argv[1], "backfill target", asyncpg=True)
snapshot = identity(sys.argv[2], "snapshot target")
restore_admin = identity(sys.argv[3], "restore administration")
restore = identity(sys.argv[4], "restore target")
restore_db = sys.argv[5]

if snapshot != target:
    raise SystemExit("snapshot target identity mismatch")
if restore_admin[:2] != restore[:2]:
    raise SystemExit("restore administration identity mismatch")
if restore[2] != restore_db:
    raise SystemExit("restore database name mismatch")
if restore == target:
    raise SystemExit("restore target must differ from operated database")
PY

mkdir -p "$BACKFILL_SNAPSHOT_DIR"
pg_dump --format=custom --no-owner --file \
  "$BACKFILL_SNAPSHOT_DIR/brain-pre-backfill.dump" "$BACKFILL_PGURL"
pg_restore --list "$BACKFILL_SNAPSHOT_DIR/brain-pre-backfill.dump" >/dev/null
sha256sum "$BACKFILL_SNAPSHOT_DIR/brain-pre-backfill.dump"

# createdb échoue si le nom existe déjà : il ne peut donc pas restaurer par erreur
# dans une base préexistante. Le contrôle SQL confirme la base isolée avant le worker.
createdb --maintenance-db="$BACKFILL_RESTORE_ADMIN_PGURL" -- "$BACKFILL_RESTORE_DB"
pg_restore --no-owner --dbname="$BACKFILL_RESTORE_PGURL" \
  "$BACKFILL_SNAPSHOT_DIR/brain-pre-backfill.dump"
psql "$BACKFILL_RESTORE_PGURL" -v ON_ERROR_STOP=1 -v restore_db="$BACKFILL_RESTORE_DB" -Atqc \
  "SELECT current_database() = :'restore_db';" | grep -qx t

"${BACKFILL_PYTHON:-python}" -m brain_v42.maintenance.embedding_backfill \
  --execute --project-key "$BACKFILL_PROJECT" --entity-type learning --entity-type decision \
  --batch-size 20 --limit 100
```
<!-- backfill-recovery-guard:end -->

Le reçu de hash, le scope, l’égalité d’identité et le restore isolé sont les
prérequis de l’exécution. Au moindre échec du worker, de validation ou
d’ambiguïté sur la cible, arrêter le canary et ne pas relancer `--execute` : la
barrière Dream reste fail-closed.

Une restitution de la base opérée exige une procédure de récupération base
entière approuvée, writers arrêtés et le snapshot validé ci-dessus. Utiliser la
base isolée restaurée comme preuve avant de préparer une base de remplacement
selon la procédure DR approuvée ; ne jamais lancer `pg_restore` in-place sur la
base opérée. Il n’existe pas de rollback granulaire par learning/decision, de
commande sûre de restauration sélective native pour ce worker, ni de moyen de
retrouver les anciens vecteurs sans le snapshot. Sans ces prérequis, s’arrêter
plutôt que d’écraser des écritures concurrentes.

## Canary Dream

Après l’application de la migration 038, exécuter deux fois EXTRACT en DRY avec
une seule fenêtre :

```bash
POSTGRES_URL=<url-asyncpg-base-isolee> \
  BRAIN_NVIDIA_API_KEY=<cle-canary> \
  python -m scripts.ticket_extract --limit 1
```

Vérifier, pour chacun des deux passages, une ligne `dream_runs` terminale sans
erreur, une tentative par ticket, aucune proposition appliquée (`--wet` absent)
et un backlog learning/decision comparable nul. Augmenter seulement le nombre
de tickets après deux canaries propres.

## Arrêt

Arrêter le canary si le backfill renvoie une erreur, si un `dream_run` terminal manque, ou si une tentative contient une cause non expurgée. Ne pas activer le timer sans validation opérateur.
