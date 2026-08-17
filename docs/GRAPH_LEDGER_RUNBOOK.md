# Runbook — PostgreSQL graph ledger et projection Neo4j

**Statut : ACTIF — CUTOVER PRODUCTION VALIDÉ LE 22 JUILLET 2026**

Les migrations 033, 034 et 035 portent le ledger canonique, le fencing v2 et l'interlock de
recovery de projection. Le cutover de l'instance de production a été explicitement autorisé puis
validé avec les quatre gates ci-dessous. Sur toute autre instance, ou lors d'un futur rebuild,
garder `GRAPH_LEDGER_WRITE_ENABLED=false` hors d'une fenêtre offline explicitement autorisée.
Pendant cette fenêtre, la rotation ne peut l'armer qu'après l'import et ses quatre préconditions,
avec tous les writers applicatifs et projecteurs normaux arrêtés. Ne rouvrir aucun writer avant la
fermeture et la revue des quatre gates.

PostgreSQL reste la source de vérité. Neo4j est une projection jetable et reconstruisible.
Ce runbook applique la décision Brain `3d3d72e4-acb7-49fe-aabb-1618e648e627`, option A
« PostgreSQL canonique + rebuild-on-doubt ». Il couvre l'upgrade, l'import legacy, le cutover
futur, l'observabilité, le restore, le rebuild et le rollback. Il ne remplace ni une preuve de
restore PostgreSQL ni une autorisation opérateur.

Deux preuves distinctes utilisent ici le mot recovery :

- **Restore PostgreSQL testé au head exactement déployé** prouve que le ledger canonique, son
  catalogue et ses triggers peuvent être récupérés dans une cible isolée. La preuve head 035
  reste historique. Le run DR-v5 `20260724_150315` renouvelle ce gate au head 037 avec un
  restore PostgreSQL 16 et 24/24 contrôles validés.
- **Recovery de projection 035** est le protocole crash-safe qui interlocke PostgreSQL et
  Neo4j pendant un rebuild. Sa présence dans le dépôt ne vaut ni preuve de restore
  PostgreSQL, ni drill complet, ni déploiement live.

## Gates obligatoires

Arrêter la procédure dès qu'un gate manque.

| Gate | État production | Preuve à renouveler |
|---|---|---|
| **Restore PostgreSQL au head déployé** | **Acquis au head 037.** Le run DR-v5 `20260724_150315` couvre huit cibles et 47 artefacts; son restore PostgreSQL 16 passe 24/24 contrôles et concorde avec l'attestation SQL indépendante. | Avant chaque nouvelle recovery, revalider un backup au head effectivement déployé et ses invariants. Aucune restauration Neo4j corrélée n'est requise. |
| **Recovery de projection 035** | **Preuve historique au head 035.** La recovery `776fd1b9-dbd0-4a1c-b7e3-cd3398ebf93a` a retourné `recovered`, puis le projecteur a armé la génération 3. | Pour tout nouvel incident, utiliser un nouvel UUID, sauf reprise d'un `recovery_id` encore actif. |
| **Isolation des writers Neo4j** | **Preuve historique du cutover 035.** Writers arrêtés, credential rotaté, ancien credential refusé, sessions legacy à zéro et clés `NEO4J_*` retirées du runtime partagé. | Reprouver la quiescence et le refus de l'ancien credential à chaque rotation ou rebuild. |
| **Rebuild complet isolé** | **Preuve historique au head 035.** Après le smoke, PostgreSQL et Neo4j concordent sur 4 678 entités, 11 888 relations et 16 566 curseurs ; outbox à zéro et onze contraintes exactes. | Refaire la comparaison complète et un smoke MCP après chaque recovery. |

Le cutover graph du 22 juillet reste historiquement validé par ses quatre preuves au head 035.
Le run DR-v5 renouvelle le gate PostgreSQL pour la production alors courante, au head 037. Toute nouvelle
recovery ou tout nouveau rebuild doit revalider cette preuve pour l'instance et le head ciblés,
puis refermer les trois autres gates avant de rouvrir un writer.
Les trois preuves Neo4j historiques ne ferment pas le rebuild Neo4j dédié de DR-v5.

La présence du code ou de tests de dépôt, unitaires comme intégration, ne ferme aucun gate.
La preuve live conservée inclut le learning smoke
`ca2fac6f-ba19-49e2-a96a-e770e8667c18`, ses quatre relations livrées dans les deux stores, le
backup DR-v3 `20260722_001955`, le run DR-v5 `20260724_150315` et leurs rapports de restore Brain
24/24. Conserver les identifiants de backup, versions déployées, sorties bornées et horodatages de
chaque nouvelle preuve.

## Configuration runtime active

```dotenv
GRAPH_ENABLED=true
GRAPH_LEDGER_WRITE_ENABLED=true
GRAPH_OUTBOX_INTERVAL_SECONDS=5
GRAPH_OUTBOX_BATCH_SIZE=100
GRAPH_OUTBOX_MAX_ATTEMPTS=10
```

Le `.env` partagé conserve les flags afin que chaque garde legacy observe le cutover. Les clés
`NEO4J_URL`, `NEO4J_USER` et `NEO4J_PASSWORD` doivent rester absentes du `.env` partagé et de tout
environnement hérité. La paire versionnée associe `brain-v42-graph-recon.timer` à
`brain-v42-graph-recon.service`. L'`ExecStart` du service lance uniquement l'inventaire PostgreSQL
read-only de `scripts/rebuild_graph_projection.py`, sans credential Neo4j. La paire peut être
planifiée après publication du service vérifié, mais ne remplace jamais la recovery 035 explicitement
attestée.

Le credential rotaté appartient uniquement au MCP et vit dans
`~/.config/brain-v42/graph-projector.env` :

```dotenv
GRAPH_PROJECTOR_ENABLED=true
GRAPH_PROJECTOR_NEO4J_URL=bolt://127.0.0.1:7687
GRAPH_PROJECTOR_NEO4J_USER=neo4j
GRAPH_PROJECTOR_NEO4J_PASSWORD=REPLACE_WITH_ROTATED_PASSWORD
```

Partir de `deploy/systemd/graph-projector.env.example`, remplacer le placeholder sans afficher
le secret, puis imposer le mode exact `0600`. Réserver le fichier aux quatre clés
`GRAPH_PROJECTOR_*` ; il doit être régulier, non symbolique et appartenir à l'utilisateur du
service. Il ne doit contenir aucune clé `NEO4J_*`. L'URI privée doit utiliser un schéma
Bolt/Neo4j accepté, sans credential, query, fragment ni chemin.

Ne pas précharger le fichier d'exemple actif lorsque `GRAPH_LEDGER_WRITE_ENABLED=false` :
`GRAPH_PROJECTOR_ENABLED=true` exige le ledger actif. L'unité MCP charge le fichier privé en
dernier. Son `ExecStartPre` contrôle tout fichier privé présent, même avec le ledger dormant,
compare le flag ledger effectif au fichier partagé et refuse un fichier requis absent, trop
permissif, mal possédé, symbolique, incomplet ou encore placeholder. Le runtime démarre ensuite
directement avec l'interpréteur attesté, sans login shell capable de remplacer ces valeurs. Ce
preflight ne prouve ni la révocation Neo4j, ni la quiescence des writers, ni zéro session ; le
gate credentials reste ouvert jusqu'aux preuves opérateur.

Le mot de passe Neo4j doit être fort, distinct de `brain_v42_graph` et absent des lignes de
commande, journaux et preuves. Ne jamais afficher `POSTGRES_URL`, les mots de passe Neo4j ni le
contenu d'un `EnvironmentFile`.

### Rotation atomique du credential Neo4j

Le CLI de rotation travaille en lecture seule sans `--apply`. Depuis n'importe quel répertoire,
lui fournir le chemin réel, absolu et non symbolique du dépôt canonique ; il lie chaque commande
Compose à ce dépôt, valide sa configuration et vérifie que le conteneur live porte le label de ce
répertoire. `--shared-env` doit désigner exactement `/ABSOLUTE/REPO/.env`, le fichier que Compose
charge automatiquement. Pour chaque commande Compose, le CLI force
`BRAIN_NEO4J_AUTH_FILE=<config-dir>/neo4j-auth` dans un environnement borné sans credential, puis
vérifie le bind mount correspondant après recréation. Le preflight ne crée aucun fichier et ne
modifie ni Neo4j ni le `.env` :

```bash
/ABSOLUTE/REPO/.venv/bin/python \
  /ABSOLUTE/REPO/scripts/rotate_neo4j_credential.py \
  --repo-root /ABSOLUTE/REPO \
  --shared-env /ABSOLUTE/REPO/.env \
  --config-dir /home/SERVICE_USER/.config/brain-v42 \
  --neo4j-uri bolt://127.0.0.1:7687
```

`--config-dir` doit être exactement `Path.home()/.config/brain-v42` pour l'utilisateur qui
exécute le CLI. Le parent `.config` doit déjà exister, appartenir à cet utilisateur et ne pas être
inscriptible par groupe/autres. Un répertoire `brain-v42` existant doit déjà être régulier,
non symbolique, possédé et en `0700` : le CLI refuse un mode incorrect au lieu de le corriger.
Le `.env` partagé doit contenir une unique affectation effective `GRAPH_ENABLED=true` et aucune
clé `GRAPH_PROJECTOR_*`, quelle que soit la casse ; ces clés appartiennent exclusivement au
fichier privé.

Si ce répertoire existe avec un autre mode, le préparer explicitement avant le preflight. Borner
chaque commande à ce chemin exact, vérifier type, absence de symlink et owner courant, puis
revalider le mode ; ne modifier ni déplacer son contenu :

```bash
test -d /home/SERVICE_USER/.config/brain-v42
test ! -L /home/SERVICE_USER/.config/brain-v42
test "$(stat -c '%u' /home/SERVICE_USER/.config/brain-v42)" -eq "$(id -u)"
chmod 0700 /home/SERVICE_USER/.config/brain-v42
test "$(stat -c '%a' /home/SERVICE_USER/.config/brain-v42)" = 700
```

Le CLI refuse toute cible autre que le Neo4j local sur le port `7687`, normalise les schémas
`bolt`/`neo4j` et les alias `localhost`/`127.0.0.1`/`::1`, puis exige qu'elle corresponde au
`NEO4J_URL` legacy. Userinfo, chemin, query et fragment sont interdits.

N'ajouter `--apply` qu'après validation des quatre **préconditions de rotation** : arrêt effectif
de tous les writers, preuve de zéro session Neo4j, cible Neo4j dédiée et restore PostgreSQL testé.
Les quatre attestations sont obligatoires et ne réalisent elles-mêmes aucune détection :

```bash
/ABSOLUTE/REPO/.venv/bin/python \
  /ABSOLUTE/REPO/scripts/rotate_neo4j_credential.py \
  --repo-root /ABSOLUTE/REPO \
  --shared-env /ABSOLUTE/REPO/.env \
  --config-dir /home/SERVICE_USER/.config/brain-v42 \
  --neo4j-uri bolt://127.0.0.1:7687 \
  --apply \
  --writers-off-confirmed \
  --neo4j-sessions-zero-confirmed \
  --neo4j-dedicated-confirmed \
  --postgres-restore-tested
```

Le CLI prend un verrou exclusif, crée un journal reprenable en `0600`, effectue la rotation avec
des paramètres Cypher sur la base `system`, puis exige que l'ancien credential soit refusé par une
erreur d'authentification et que le nouveau soit accepté. Il installe `neo4j-auth` en `0644` dans
le répertoire `0700` et `graph-projector.env` en `0600`. Ensuite seulement, une écriture atomique
retire exactement `NEO4J_URL`, `NEO4J_USER` et `NEO4J_PASSWORD` du `.env`, réduit toutes les
affectations `GRAPH_LEDGER_WRITE_ENABLED` à l'unique valeur `true`, puis recrée **uniquement** le
service Compose `neo4j`. Le script ne démarre aucun writer ni aucune unité systemd.

Chaque sortie est un objet JSON de statut sans secret. Ne jamais afficher le journal
`.neo4j-rotation-state` : il contient le matériel nécessaire à une reprise. Si la recréation ou
sa validation échoue, le `.env` partagé est restauré, le journal reste en place et tous les
writers doivent rester arrêtés. Corriger la cause, puis reprendre avec la même commande et
`--resume`. Ne jamais relancer sans `--resume`, supprimer le journal manuellement, ni réintroduire
l'ancien credential.

Le statut `rotated` ne ferme pas à lui seul les gates **Recovery de projection 035** et **Rebuild
complet isolé**. La rotation/révocation les précède nécessairement. Exécuter ensuite la recovery
et le rebuild décrits plus bas, conserver leurs preuves et n'ouvrir aucun writer avant fermeture
des quatre gates du tableau.

Le projecteur dimensionne son lease à au moins deux intervalles de poll. Abaisser
`GRAPH_OUTBOX_MAX_ATTEMPTS` classe au prochain claim les événements déjà au-dessus de la
nouvelle limite en `max_attempts`; vérifier `exhausted` avant et après tout changement.

Avec le flag ouvert, le démarrage vérifie :

- les six tables `projects`, `project_aliases`, `brain_entities`, `entity_relations`,
  `graph_outbox` et `graph_projection_leases` ;
- le slot `neo4j` en protocole 2, son invariant de génération armée et ses phases recovery ;
- les colonnes `graph_outbox.lease_generation`, `graph_outbox.claim_version`,
  `graph_projection_leases.recovery_id`, `recovery_phase` et
  `last_completed_recovery_id` ;
- la contrainte validée `graph_projection_leases_recovery_state_valid`.

Le démarrage crée ensuite onze contraintes d'identité Neo4j, puis lance le projecteur. Il ne
vérifie pas le head Alembic exact, tous les triggers 033, les indexes ni la cohérence
PostgreSQL–Neo4j. La validation locale refuse les credentials legacy dans le rôle projecteur,
mais ne prouve pas leur révocation côté Neo4j ou leur retrait des anciens processus. Les
contrôles opérateur restent obligatoires.

## Upgrade initial vers le head 035 — procédure historique

Cette phase documente le cutover initial du 22 juillet 2026 et installe le schéma graph sans
changer le propriétaire runtime. Ne pas la rejouer sur la production actuelle : mesurez son head
avant toute action, il dépasse déjà 035, et ne jamais downgrader pour l'atteindre. Sur une
nouvelle instance, finir ensuite la chaîne Alembic
jusqu'au head courant avec le runbook de migration principal avant d'activer le runtime courant.

1. Garder `GRAPH_LEDGER_WRITE_ENABLED=false`.
2. Identifier explicitement l'hôte et la base PostgreSQL ciblés, puis enregistrer le head
   courant.
3. Prendre une sauvegarde PostgreSQL pré-upgrade restaurable pour le rollback de la fenêtre.
   Elle ne ferme pas le gate de restore au head 035.
4. Arrêter MCP, Dream, automation, les clients stdio, les scripts de maintenance et tout
   ancien projecteur. Enregistrer auparavant l'état `enabled` des timers afin de le restaurer
   après la fenêtre.
5. Prévoir les verrous : 033 verrouille les tables sources en
   `SHARE ROW EXCLUSIVE` ; 034 modifie `graph_outbox` et réinitialise les leases des événements
   non livrés ; 035 modifie `graph_projection_leases`, ajoute trois colonnes et une contrainte
   d'état recovery.
6. Appliquer exactement 035 :

   ```bash
   BRAIN_ALEMBIC_ALLOW_PROD=1 alembic upgrade 035
   ```

   `BRAIN_ALEMBIC_ALLOW_PROD=1` contourne volontairement la garde de production. L'utiliser
   pour cette commande seulement, après vérification de la cible ; ne jamais l'exporter dans
   le shell.

7. Vérifier le head et les six tables :

   ```sql
   SELECT version_num FROM alembic_version;

   SELECT to_regclass(names.name) AS relation
   FROM unnest(ARRAY[
       'public.projects',
       'public.project_aliases',
       'public.brain_entities',
       'public.entity_relations',
       'public.graph_outbox',
       'public.graph_projection_leases'
   ]) AS names(name);
   ```

   `version_num` doit valoir `035` et aucune relation ne doit être `NULL`.

8. Vérifier le fencing PostgreSQL :

   ```sql
   SELECT slot, protocol_version, generation, owner, leased_until,
          neo4j_armed_generation, recovery_id, recovery_phase,
          last_completed_recovery_id
   FROM graph_projection_leases
   WHERE slot = 'neo4j';

   SELECT column_name, is_nullable
   FROM information_schema.columns
   WHERE table_schema = 'public'
     AND table_name = 'graph_outbox'
     AND column_name IN ('lease_generation', 'claim_version')
   ORDER BY column_name;

   SELECT conname, convalidated
   FROM pg_constraint
   WHERE conname = 'graph_projection_leases_recovery_state_valid';
   ```

   Exiger une ligne `neo4j`, `protocol_version=2` et
   `neo4j_armed_generation IS NULL OR neo4j_armed_generation=generation`. Hors recovery active,
   exiger aussi `recovery_id IS NULL`, `recovery_phase='idle'`, les deux colonnes outbox et une
   contrainte `convalidated=true`.

9. Après toutes les vérifications au head 035, prendre une nouvelle sauvegarde PostgreSQL
   post-upgrade. Restaurer **cette sauvegarde 035**, sans migration corrective dans la sandbox,
   dans une cible isolée ; exiger `alembic_version=035`, puis contrôler le catalogue complet,
   les triggers 033 et les invariants 034–035. Conserver la preuve secret-free. Tant que ce
   restore n'a pas réussi, le gate PostgreSQL reste ouvert. Ne pas restaurer Neo4j pour fermer
   ce gate.

Des événements outbox non livrés sont attendus avec le flag fermé.

## Importer le graphe legacy

L'import lit un snapshot borné de Neo4j, normalise les identités et propriétés autorisées,
puis écrit les faits absents dans PostgreSQL. Il est autorisé uniquement avant la première
écriture canonical-only. Après cette frontière, ne jamais réimporter Neo4j vers PostgreSQL :
la projection peut être détruite et reconstruite, mais elle ne décide plus du canonique.

1. Prévisualiser :

   ```bash
   uv run python scripts/backfill_graph_ledger.py
   ```

2. Si `truncated_nodes` ou `truncated_relations` vaut vrai, augmenter explicitement
   `--max-nodes` ou `--max-relations`, puis recommencer.
3. Un skip non expliqué bloque l'activation. Ne jamais utiliser `--allow-skips` comme preuve
   de cutover.
4. Pour l'import final, maintenir tous les writers et projecteurs arrêtés, puis relancer le
   dry-run.
5. Appliquer seulement sans skip ni troncature :

   ```bash
   uv run python scripts/backfill_graph_ledger.py \
     --apply \
     --writers-off-confirmed
   ```

`--writers-off-confirmed` est une déclaration opérateur, pas une détection. Un retour `0`
indique un snapshot complet, `1` un rapport incomplet et `2` un refus ou un échec. Les faits
nouvellement importés reçoivent un événement initial livré parce qu'ils existent déjà dans
Neo4j. Les faits PostgreSQL préexistants restent pending afin de converger au cutover.

## Cutover futur

Ouvrir cette fenêtre uniquement avec une autorisation opérateur. Les étapes de rotation,
recovery et rebuild ci-dessous ferment les gates dans cet ordre ; ne démarrer aucun writer avant
que les quatre preuves du tableau soient valides pour l'instance et le head exactement déployés.

1. Enregistrer les révisions déployées, le head exactement déployé — mesuré avant la fenêtre,
   jamais recopié d'une exécution précédente —, la configuration effective sans secret, les
   comptes PostgreSQL/Neo4j et l'identifiant de la sauvegarde PostgreSQL dont le restore isolé a
   été testé à ce même head.
   Une sauvegarde Neo4j n'est pas un gate de l'option A.
2. Arrêter Dream, MCP, automation, les clients stdio et tous les writers directs. Arrêter aussi
   `brain-v42-graph-recon.timer` et le garder arrêté si le fragment effectif
   `brain-v42-graph-recon.service` utilise encore `--fix`.
3. Refaire le dry-run legacy, puis l'import final.
4. Exécuter le preflight puis la rotation atomique décrite plus haut avec les quatre
   préconditions de rotation attestées.
   Son statut `rotated` prouve le refus authentifié de l'ancien credential, l'acceptation du
   nouveau, l'installation des deux fichiers, le retrait des trois clés legacy, l'unique
   `GRAPH_LEDGER_WRITE_ENABLED=true`, la recréation isolée de Neo4j et ses métadonnées propres.
5. Vérifier qu'aucune unité ni aucun binaire legacy ne distribue encore de clé `NEO4J_*`,
   régénérer le service systemd depuis le checkout compatible avec le head exactement déployé et
   le protocole graph 035, puis exécuter `systemctl --user daemon-reload` et inspecter le fragment
   effectif :

   ```bash
   systemctl --user cat brain-v42-graph-recon.service
   ```

   L'`ExecStart` effectif doit appeler exclusivement `<repo>/.venv/bin/python
   <repo>/scripts/rebuild_graph_projection.py`, sans `--fix`, `reconcile_graph_drift` ni
   `recover_graph_projection.py`. Avec Neo4j Community, l'isolation des writers repose sur la
   distribution et la révocation du secret, pas sur un rôle RBAC fin.
6. Vérifier dans le `.env` partagé, sans afficher de secret, que `GRAPH_ENABLED=true` est conservé
   et que `GRAPH_LEDGER_WRITE_ENABLED=true` apparaît exactement une fois.

7. Sans rouvrir de writer, poursuivre le protocole **Incident et recovery de projection 035** avec
   un UUID neuf. Pour ce premier cutover, la rotation de l'étape 4 satisfait déjà l'arrêt, la
   révocation legacy et l'installation du secret privé demandés par les étapes 1, 2 et le début de
   l'étape 7 du protocole d'incident : réutiliser ce secret, ne pas le révoquer ni relancer la
   rotation. Reprendre à la capture de l'étape 3, exécuter les étapes 4 à 6, puis à l'étape 7 lancer
   seulement le preflight et la recovery. Mener le rebuild complet isolé jusqu'à convergence et
   archiver les preuves de reprise crash-safe, reset borné, reconstruction depuis PostgreSQL et
   compteurs convergés. Ces preuves ferment les deux gates restants ; un statut `rotated` ne les
   remplace pas.
8. Après revue des quatre preuves seulement, démarrer un seul MCP :

   ```bash
   systemctl --user start brain-mcp-http.service
   ```

9. Vérifier que le preflight a réussi, puis vérifier le démarrage, les onze contraintes Neo4j,
   le lease PostgreSQL et le fence Neo4j. Aucun log `fence_rejected`, `history_conflict` ou
   `batch_failed` n'est accepté. Un preflight vert ne ferme pas le gate credentials.
10. Sur `/metrics`, exiger `database.graph_outbox.available=true`, `pending=0`, `claimed=0`,
   `exhausted=0`, `oldest_pending_age_seconds=0`, `projector.healthy=true` et
   `projector.recovery_active=false` sur une fenêtre stable. Effectuer ensuite une écriture MCP
   bornée dans un projet de test, puis prouver le fait dans PostgreSQL et sa projection dans
   Neo4j.
11. Redémarrer les producteurs un par un. Vérifier leur flag effectif avant chaque démarrage
    et observer l'outbox. Réactiver Dream en dernier. Ne jamais remettre le nouveau
    credential à un ancien writer.

Avec le flag ouvert, `scripts/init_graph.py`, `scripts/reconcile_graph.py --fix` et
`scripts.dream.reconcile_graph_drift --fix` doivent sortir avec le code `2`. Cette garde
locale ne remplace ni la rotation du credential ni la quiescence.

## Observabilité

Le CLI fournit un inventaire en lecture seule :

```bash
uv run python scripts/rebuild_graph_projection.py
```

Cette commande exige le schéma 035. Son ancien mode `--apply` est retiré et refuse toute
mutation ; seul `scripts/recover_graph_projection.py` porte le protocole de recovery.

### PostgreSQL

```sql
SELECT
    count(*) FILTER (
        WHERE delivered_at IS NULL
          AND last_error_code IS DISTINCT FROM 'max_attempts'
    ) AS pending,
    count(*) FILTER (
        WHERE delivered_at IS NULL
          AND last_error_code = 'max_attempts'
    ) AS exhausted,
    min(created_at) FILTER (
        WHERE delivered_at IS NULL
          AND last_error_code IS DISTINCT FROM 'max_attempts'
    ) AS oldest_pending
FROM graph_outbox;

SELECT operation, attempt_count, COALESCE(last_error_code, 'none') AS error_code,
       lease_generation, claim_version, lease_owner, leased_until, count(*)
FROM graph_outbox
WHERE delivered_at IS NULL
GROUP BY operation, attempt_count, COALESCE(last_error_code, 'none'),
         lease_generation, claim_version, lease_owner, leased_until
ORDER BY operation, attempt_count, error_code, leased_until;

SELECT clock_timestamp() AS db_now,
       slot, protocol_version, generation, neo4j_armed_generation,
       owner, leased_until, recovery_id, recovery_phase,
       last_completed_recovery_id,
       CASE
           WHEN recovery_id IS NOT NULL THEN 'recovery_' || recovery_phase
           WHEN neo4j_armed_generation = generation THEN 'armed'
           WHEN leased_until > clock_timestamp() THEN 'activating'
           ELSE 'unarmed_requires_recovery_check'
       END AS state
FROM graph_projection_leases
WHERE slot = 'neo4j';
```

Une génération PostgreSQL non armée exige toujours une comparaison avec Neo4j. Ne pas
déduire une réparation de la seule ligne PostgreSQL.

### Neo4j

```cypher
MATCH (fence:BrainProjectionFence {name: 'canonical'})
RETURN fence.protocol_version AS protocol_version,
       fence.generation AS generation,
       fence.owner_id AS owner_id,
       fence.recovery_id AS recovery_id;

MATCH (cursor:BrainProjectionCursor)
RETURN count(cursor) AS cursor_count,
       max(cursor.updated_at) AS latest_cursor_update;
```

L'absence du fence bloque le runtime normal : démarrer MCP ne le crée plus. Elle n'est acceptée
que sur une cible Neo4j vide et dédiée pendant une recovery 035 autorisée. Un leader PostgreSQL
normal non armé ne peut avancer que depuis la génération Neo4j immédiatement précédente ; un
leader déjà armé exige sa génération exacte. Une divergence persistante, un owner conflict,
un marqueur recovery inattendu ou un historique de curseur incompatible bloque la procédure.
Ce contrôle ne détecte pas une PITR Neo4j dans la même génération ; tout restore connu impose
donc une recovery et une requeue complètes.

### Logs

```bash
journalctl --user -u brain-mcp-http.service --since '-30 min' --no-pager \
  | rg 'graph_outbox_projector|fence_rejected|history_conflict|batch_failed'
```

Traiter immédiatement :

- `fence_rejected` : génération incohérente ou activation incomplète ;
- `history_conflict` : même révision, historique différent ;
- `batch_failed` répété : projection indisponible ;
- `exhausted>0`, croissance de `pending` ou vieillissement de `oldest_pending`.

Le endpoint JSON `/metrics` expose aussi un bloc à cardinalité fixe :

```json
{
  "database": {
    "graph_outbox": {
      "available": true,
      "pending": 0,
      "ready": 0,
      "claimed": 0,
      "exhausted": 0,
      "oldest_pending_age_seconds": 0.0,
      "projector": {
        "generation": 12,
        "armed": true,
        "lease_active": true,
        "recovery_active": false,
        "healthy": true
      }
    }
  }
}
```

`pending` inclut les retries différés mais exclut `exhausted`; `ready` compte les événements
temporellement disponibles et sans lease vivant, même si l'ordre d'un aggregate peut encore
les bloquer, et `claimed` ceux dont le lease est vivant. La différence
`pending - ready - claimed` regroupe donc le backoff programmé et les révisions bloquées par
ordre. `projector.healthy` signifie
« génération armée, lease vivant, aucune recovery active » ; ce signal ne prouve pas à lui
seul le contenu Neo4j. `available=false` est un hard no-go : les zéros par défaut associés ne
constituent aucune preuve. Le dépôt conserve ce contrat JSON existant et n'ajoute pas de
dépendance `prometheus_client`.

## Incident et recovery de projection 035

Une génération PostgreSQL non armée, un `history_conflict`, une projection perdue ou douteuse,
ou un marqueur recovery interrompu imposent le protocole 035. Ne jamais forcer l'armement, modifier
une génération, supprimer le fence, vider Neo4j ou réinitialiser les curseurs manuellement.
Le seul chemin mutant publié pour une recovery/rebuild est
`scripts/recover_graph_projection.py`.

Cette procédure exige d'abord un restore PostgreSQL au head exactement déployé — mesuré avant la
procédure, jamais recopié d'une exécution précédente —, l'isolation des writers, zéro session et
une cible Neo4j dédiée. Sa
complétion crash-safe et la convergence du rebuild ferment
ensuite les deux gates correspondants. Le flag `--postgres-restore-tested` est une déclaration
opérateur ; la migration 035 ou un test vert ne la satisfait pas.

1. Arrêter MCP, Dream, automation, clients stdio, maintenance et tous les writers/projecteurs.
2. Révoquer le credential Neo4j courant, retirer sa distribution legacy et attester zéro
   session active sur la base Neo4j ciblée, tous credentials confondus.
3. Capturer sans mutation le lease/outbox PostgreSQL, le fence/cursors Neo4j, les logs, les
   versions et l'identifiant de sauvegarde PostgreSQL.
4. Vérifier la preuve d'un restore PostgreSQL isolé au head exactement déployé, avec les
   invariants graph 033–035 et tous les objets des révisions suivantes. Si cette preuve manque,
   si elle vise un head antérieur ou si le restore a échoué, arrêter; ne jamais downgrader pour
   fermer ce gate.
5. Prouver que la base Neo4j ciblée est dédiée à Brain. L'option A traite cette base comme une
   projection jetable : partir d'une cible vide ou accepter que la recovery efface uniquement
   les labels Brain allowlistés et les cursors. Une sauvegarde Neo4j n'est pas exigée et ne
   doit jamais servir à rétablir l'état canonique.
6. Générer et archiver un UUID unique pour cet incident. Si PostgreSQL contient déjà un
   `recovery_id`, reprendre exactement celui-ci, même après expiration de son lease ; un autre
   UUID est refusé. Ne pas réutiliser l'UUID d'une ancienne recovery après une recovery plus
   récente : seule la dernière complétion est mémorisée.
7. Installer un secret neuf uniquement dans le fichier privé MCP. Dans le `.env` partagé,
   configurer `GRAPH_ENABLED=true` et `GRAPH_LEDGER_WRITE_ENABLED=true`, sans démarrer de
   service. Remplacer les chemins absolus et l'UUID ci-dessous, exécuter le preflight, puis
   lancer une unité utilisateur transitoire qui charge les deux `EnvironmentFile` sans les
   interpréter comme du code shell :

   ```bash
   /ABSOLUTE/REPO/.venv/bin/python \
     /ABSOLUTE/REPO/scripts/check_graph_projector_env.py \
     --shared /ABSOLUTE/REPO/.env \
     --private /home/SERVICE_USER/.config/brain-v42/graph-projector.env

   systemd-run --user --wait --pipe --collect --service-type=exec \
     --unit=brain-v42-graph-recovery \
     --working-directory=/ABSOLUTE/REPO \
     --property=EnvironmentFile=/ABSOLUTE/REPO/.env \
     --property=EnvironmentFile=/home/SERVICE_USER/.config/brain-v42/graph-projector.env \
     /ABSOLUTE/REPO/.venv/bin/python \
     /ABSOLUTE/REPO/scripts/recover_graph_projection.py \
     --apply \
     --recovery-id UUID_GENERATED_FOR_THIS_INCIDENT \
     --writers-off-confirmed \
     --legacy-credential-revoked-confirmed \
     --neo4j-sessions-zero-confirmed \
     --neo4j-dedicated-confirmed \
     --postgres-restore-tested
   ```

   Les cinq confirmations sont des assertions opérateur, pas des contrôles automatiques. Avant
   de créer l'interlock PostgreSQL, le CLI vérifie la connectivité privée à Neo4j, puis
   demande la création des contraintes de projection. Un échec à ce stade refuse la recovery
   sans interlock PostgreSQL. Auditer séparément `SHOW CONSTRAINTS` : le CLI ne relit pas leur
   définition lorsqu'un nom existe déjà.

   Le lease vaut 3600 secondes par défaut et accepte une valeur explicite de 60 à 86400 via
   `--lease-seconds`. **DANGER :** le reset supprime les nœuds portant les labels Brain
   `Project`, `Domain`, `Decision`, `Learning`, `Snippet`, `Runbook`, `ADR`, `Feature` ou
   `Plan`, ainsi que les `BrainProjectionCursor`, avant de les reconstruire depuis PostgreSQL.
   Il ne supprime pas les autres labels, mais la base dédiée reste obligatoire.
8. Si le processus s'interrompt, relancer la commande complète avec le même UUID. Le protocole
   reprend `prepared` ou `neo_ready` sans nouveau bump ni nouvelle requeue. En `neo_ready`, il
   rejoue **toujours** le reset borné avant de finaliser : un fence ou des cursors survivants ne
   prouvent pas l'intégrité du contenu. Une cible vide, un marker exact, une génération plus
   ancienne compatible ou le fence exact déjà finalisé par le même owner sont acceptés ; un
   fence futur, un mauvais protocole ou un marker étranger reste refusé. Une reprise après
   complétion retourne `already_completed` sans toucher Neo4j. Si PostgreSQL est revenu à
   `idle`/`completed` et que Neo4j a ensuite été perdu, ouvrir un nouvel incident avec un nouvel
   UUID : ne jamais rejouer l'ancien UUID complété.
9. Archiver le JSON secret-free. Le statut `recovered` signifie « reset et requeue finalisés »,
   pas « projection déjà convergée ». La préparation incrémente la génération et requeue les
   révisions dans une transaction PostgreSQL ; le reset efface les labels Brain bornés et les
   cursors, puis installe le marqueur recovery dans une transaction Neo4j ; la finalisation
   retire les deux interlocks. Un fence plus récent, un mauvais protocole ou un autre UUID doit
   rester un refus fail-closed.
10. Démarrer un seul MCP avec le ledger et le credential privé actifs. Vérifier fence, lease,
    logs, `pending=0`, `exhausted=0`, puis comparer les comptes canoniques aux nœuds métier
    Neo4j :

    ```sql
    SELECT count(*) AS entity_count
    FROM brain_entities
    WHERE lifecycle <> 'deleted';

    SELECT count(*) AS relation_count
    FROM entity_relations
    WHERE lifecycle = 'active';
    ```

    ```cypher
    MATCH (node)
    WHERE NOT node:BrainProjectionFence
      AND NOT node:BrainProjectionCursor
    RETURN count(node) AS entity_count;

    MATCH ()-[relation]->()
    RETURN count(relation) AS relation_count;
    ```

    Les nœuds `BrainProjectionFence` et `BrainProjectionCursor` sont des contrôles internes ;
    les compter comme entités rendrait la comparaison fausse.
11. Échantillonner chaque type de relation et effectuer un smoke test MCP borné avant de
    rouvrir les producteurs un par un.

## Restore

PostgreSQL est l'unique autorité de restore. Neo4j est reconstruit depuis cet état et n'est
jamais restauré comme participant corrélé. Le run DR-v5 `20260724_150315` satisfait ce prérequis
pour la production alors courante, au head 037. Tout nouveau backup, head ou environnement doit
obtenir sa propre validation isolée avant d'appliquer cette procédure.

1. Arrêter tous les owners et révoquer leur credential Neo4j.
2. Restaurer PostgreSQL dans une cible isolée.
3. Exiger le head exactement déployé — mesuré avant la procédure, jamais recopié d'une exécution
   précédente — et vérifier le catalogue, les triggers 033, les contraintes 034–035, les objets
   036–037 et les invariants applicatifs.
   Conserver la preuve; si le restore isolé échoue ou annonce un head antérieur, arrêter.
4. Désigner explicitement la cible restaurée comme nouvelle autorité canonique avant toute
   mutation. Créer pour cette fenêtre un fichier d'environnement partagé dédié dont
   `POSTGRES_URL` cible cette instance ; ne pas réutiliser implicitement le `.env` live. Depuis
   ce même environnement, enregistrer sans secret `current_database()`, `inet_server_addr()`,
   `inet_server_port()` et la valeur exacte attendue de `alembic_version.version_num`. Dans la
   commande de recovery de la section précédente, remplacer chaque `/ABSOLUTE/REPO/.env` par ce
   fichier dédié. Toute ambiguïté sur l'endpoint ou la base est un hard no-go.
5. Ne pas démarrer le projecteur et ne pas réutiliser un Neo4j potentiellement plus récent.
   Préparer une base Neo4j dédiée et vide avec un credential neuf.
6. Examiner le singleton PostgreSQL restauré. Attendre l'expiration d'un lease runtime sans le
   réécrire. Si un `recovery_id` est déjà présent, reprendre cet UUID exact au lieu d'en créer
   un autre.
7. Choisir l'UUID depuis PostgreSQL. Si `recovery_id` est actif en `prepared` ou `neo_ready`,
   reprendre exactement cet UUID. La reprise `neo_ready` rejoue systématiquement le reset borné
   sur la cible vide, ancienne ou exacte compatible ; elle refuse un fence plus récent, un
   mauvais protocole ou un marker étranger. Si PostgreSQL est `idle` et la dernière recovery est
   seulement mémorisée dans `last_completed_recovery_id`, générer un nouvel UUID : l'ancien
   retournerait `already_completed` sans reconstruire Neo4j.
8. Émettre un credential neuf, prouver zéro session active sur la base ciblée, installer le
   fichier privé, puis exécuter la recovery 035 de la section précédente. Elle requeue toutes
   les révisions et reconstruit Neo4j depuis PostgreSQL certifié.
9. Démarrer un seul MCP. Exiger fence et lease finalisés, convergence, comptes corrigés et
   smoke test avant de rouvrir les writers.

Ne jamais restaurer Neo4j ni réimporter son contenu vers PostgreSQL. Toute perte, PITR ou doute
sur la projection impose une recovery 035 complète depuis le PostgreSQL courant. Même
génération ne signifie pas même contenu : le runtime ne détecte pas une PITR
intra-génération.

## Rollback

### Rollback runtime

Conserver le schéma au head exactement déployé — mesuré avant le rollback, jamais recopié d'une
exécution précédente. Un rollback du runtime graph n'autorise aucun downgrade Alembic.

La production a franchi la première écriture canonical-only le 22 juillet 2026 avec le smoke
`ca2fac6f-ba19-49e2-a96a-e770e8667c18`. La branche legacy conditionnelle de l'étape 2 et l'étape 3
décrivent uniquement une fenêtre pré-canonical et ne sont plus applicables à cette instance. La
révocation du credential projecteur reste obligatoire ; PostgreSQL demeure l'autorité et tout
rebuild passe par une recovery 035.

1. Arrêter Dream, MCP, automation et tous les writers.
2. Révoquer le credential du projecteur. Si le chemin legacy doit reprendre, émettre un
   credential de rollback neuf ; ne jamais réactiver l'ancien secret.
3. Avant la première écriture canonical-only, le chemin legacy peut reprendre avec un secret
   neuf après retrait du fichier privé projecteur, passage de
   `GRAPH_LEDGER_WRITE_ENABLED=false` et vérification de la cohérence.
4. Dès qu'une écriture canonical-only a été acceptée, PostgreSQL reste l'autorité. **Ne pas**
   restaurer Neo4j, réimporter sa projection ou simplement remettre
   `GRAPH_LEDGER_WRITE_ENABLED=false`. Corriger le ledger puis exécuter une recovery 035 vers
   une base Neo4j dédiée et vide.
5. Redémarrer ensuite un unique propriétaire compatible avec la branche retenue, puis rouvrir
   les producteurs un par un. Un fichier privé projecteur encore présent avec
   `GRAPH_PROJECTOR_ENABLED=false` est refusé par le preflight ; le laisser à `true` avec le
   ledger fermé est également invalide.
6. Ne réactiver `brain-v42-graph-recon.timer` qu'après avoir inspecté le fragment effectif
   `brain-v42-graph-recon.service` après `daemon-reload` et validé l'`ExecStart` read-only exact
   ci-dessus. Ne jamais rediriger ce service vers la recovery 035.

Les tables, triggers et contraintes 033–035 restent en place. Avec le flag fermé, l'outbox
peut continuer à recevoir les événements des tables métier sans projecteur. Les relations écrites
directement dans Neo4j peuvent rester absentes du ledger ; tout nouveau cutover exige les
quatre gates de l'option A et une nouvelle réconciliation legacy.

### Downgrade de schéma

Un downgrade n'est jamais un rollback runtime :

- un downgrade vers 034 supprime l'interlock recovery 035 ; le runtime ledger actif actuel
  refuse ce schéma ;
- un downgrade vers 033 supprime le lease v2 et les colonnes de claims ; le runtime ledger
  actuel ne peut plus démarrer ;
- un downgrade vers 032 supprime ensuite les cinq tables 033, soit les six tables
  graph/fencing au total, leurs faits et l'historique outbox ;
- la normalisation des alias déjà appliquée aux clés projet n'est pas restaurée.

N'exécuter un downgrade qu'après sauvegarde, export du ledger, arrêt complet, révocation des
credentials et autorisation explicite de perte.

Après tout rollback, conserver : configuration sans secret, état des services et timers,
head Alembic, générations PostgreSQL/Neo4j, compteurs, inventaire outbox, credentials révoqués,
identifiants de backups et résultat du smoke test.
