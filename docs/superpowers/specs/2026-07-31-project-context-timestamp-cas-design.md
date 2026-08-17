# CAS de timestamp pour `project_contexts`

**Date :** 2026-07-31

**Statut :** décision approuvée

**Ticket :** `44ee7643-fb06-4186-a364-cb175610b973`

## Décision et preuve racine

La réparation doit signer puis rejouer exactement `updated_at` avec `plan_scan_paths`. PostgreSQL
réel a donné trois preuves conformes et deux échecs : le trigger partagé `BEFORE UPDATE` a écrasé
le timestamp signé de `+23,727 µs`, puis `finalize` a retourné `context_cas_conflict`.

La cause est `trg_project_contexts_updated`, attaché à la fonction générique
`update_updated_at`, partagée par huit tables. Après la migration Dream 038, la migration 039
donne une fonction dédiée à `project_contexts` sans modifier la fonction historique ni ses sept
autres liaisons : `decisions`,
`learnings`, `snippets`, `runbooks`, `adrs`, `features` et `indexed_plans`.

## Objectifs et non-objectifs

La solution conserve un CAS exact, restaure exactement les chemins et le timestamp non nul du
snapshot, préserve l’auto-timestamp des writers ordinaires et isole le changement à
`project_contexts`.

`updated_at` est `NOT NULL`. Le contrat ne promet ni conservation ni rollback de `NULL` : sous le
GUC exact `on`, une valeur explicite `NULL` échoue atomiquement ; sans GUC ou avec une valeur
invalide, elle est remplacée par `CURRENT_TIMESTAMP`. Le changement ne modifie pas le corpus de
plans, les phases opérateur, les sept autres tables, ni le reçu de sauvegarde.

## Alternatives rejetées

| Alternative | Rejet |
| --- | --- |
| Reçu de sauvegarde avec `RETURNING` | Trop complexe et incomplet pour prouver le rollback exact des deux champs. |
| CAS qui ignore `updated_at` | Affaiblit le contrat de concurrence. |
| Modification de `update_updated_at` | Interdite : huit tables partagent cette fonction. |

## Architecture retenue

La migration `039`, avec `down_revision = "038"`, crée sans `OR REPLACE` la fonction
`public.set_project_context_updated_at()` en `SECURITY INVOKER`. Elle remappe le trigger existant
`trg_project_contexts_updated` vers cette fonction.

La fonction dédiée est exactement `public.set_project_context_updated_at`, avec `pronargs = 0`,
retour `trigger`, `prosecdef = false`, volatilité `VOLATILE` et parallélisme `PARALLEL UNSAFE`.
Le DDL suivant est sa source canonique :

```sql
CREATE FUNCTION public.set_project_context_updated_at()
RETURNS trigger
LANGUAGE plpgsql
VOLATILE
PARALLEL UNSAFE
SECURITY INVOKER
AS $function$
BEGIN
    IF current_setting('brain_v42.allow_explicit_project_context_updated_at', true) = 'on' THEN
        IF NEW.updated_at IS NULL THEN
            RAISE EXCEPTION USING
                ERRCODE = '23502',
                MESSAGE = 'explicit_project_context_updated_at_null';
        END IF;
        RETURN NEW;
    END IF;
    NEW.updated_at := CURRENT_TIMESTAMP;
    RETURN NEW;
END;
$function$;
```

La représentation canonique est l’octet UTF-8 brut de `pg_proc.prosrc`, sans normalisation. La
requête de comparaison est :

```sql
SELECT
    encode(pg_catalog.sha256(pg_catalog.convert_to(prosrc, 'UTF8')), 'hex') AS prosrc_sha256,
    octet_length(pg_catalog.convert_to(prosrc, 'UTF8')) AS prosrc_octets
FROM pg_catalog.pg_proc
WHERE oid = 'public.update_updated_at()'::regprocedure;
```

Le même prédicat avec `public.set_project_context_updated_at()` compare la fonction dédiée. Les
hashes littéraux PostgreSQL 16.14 observés sont :

| Fonction | SHA-256 `prosrc` | Octets |
| --- | --- | ---: |
| `public.update_updated_at()` | `83ca0f7a3230405dae8b4f4e692b4983869b58e4225b6e60bbf96db3f6ae9a59` | 96 |
| `public.set_project_context_updated_at()` | `60c6154d6230d1d0e9244d8f20bc6d6b30e887e71263692e54363c96e22c0419` | 391 |

Le `prosrc` dédié commence et finit par LF. Ses attributs observés sont schéma `public`, langage
`plpgsql`, `prokind = 'f'`, `provolatile = 'v'`, `proparallel = 'u'`, `prosecdef = false`,
`proleakproof = false`, `proisstrict = false`, `proretset = false`, `pronargs = 0`,
`pronargdefaults = 0`, `proargtypes` vide, `prorettype = trigger` et `proconfig IS NULL`. La
fonction historique possède les mêmes attributs pertinents déjà capturés ; owner et ACL restent des
contrats séparés. `pg_get_functiondef` est conservé pour le diagnostic, jamais pour le hash.

Les captures proviennent de conteneurs PostgreSQL 16.14 jetables, sans accès production. Les
ressources ont été supprimées et leur absence vérifiée. Cette preuve de fonction ne constitue pas
une attestation de recovery.

Avec le GUC exactement `on`, tout timestamp non nul est conservé, même égal à `OLD.updated_at`.
Seul `NEW.updated_at IS NULL` sous ce GUC échoue atomiquement en `23502`. Sans GUC, ou avec une
valeur autre que `on`, y compris `NULL`, la fonction force `CURRENT_TIMESTAMP` et l’UPDATE réussit.

### Fonction et triggers historiques immuables

La migration 001 a établi `public.update_updated_at()` ; 039 ne la réécrit pas. Son identité
canonique est `pronargs = 0`, `prokind = 'f'`, `LANGUAGE plpgsql`, retour `trigger`, `VOLATILE`,
`PARALLEL UNSAFE`, `SECURITY INVOKER`, et ce corps :

```sql
BEGIN
    NEW.updated_at = NOW();
    RETURN NEW;
END;
```

Le preflight upgrade/downgrade et v4 comparent attributs et hash `prosrc` littéral à ce canon, afin
de détecter un `CREATE OR REPLACE` drifté même au même OID. Les sept mappings historiques exacts
sont :

| Table | Trigger | Fonction |
| --- | --- | --- |
| `decisions` | `trg_decisions_updated` | `public.update_updated_at()` |
| `learnings` | `trg_learnings_updated` | `public.update_updated_at()` |
| `snippets` | `trg_snippets_updated` | `public.update_updated_at()` |
| `runbooks` | `trg_runbooks_updated` | `public.update_updated_at()` |
| `adrs` | `trg_adrs_updated` | `public.update_updated_at()` |
| `features` | `set_features_updated_at` | `public.update_updated_at()` |
| `indexed_plans` | `set_indexed_plans_updated_at` | `public.update_updated_at()` |

Chaque ligne respecte le contrat trigger exact. La fonction dédiée est liée uniquement à
`public.project_contexts` / `trg_project_contexts_updated`.

`RepairStore.apply_paths` et `RepairStore.rollback_before_finalize` exécutent `SET LOCAL
brain_v42.allow_explicit_project_context_updated_at = 'on'` seulement après identité, head,
preuve et CAS validés, immédiatement avant une mutation réelle. Un replay `already_*` ne l’active
pas. Chaque `UPDATE` fournit `updated_at`, utilise `RETURNING updated_at` et compare la valeur
retournée à la valeur signée avant commit. Succès, erreur ou rollback réinitialisent le GUC à la fin
de transaction. Ce marqueur coordonne le trigger ; il n’authentifie rien. Writers off, snapshot,
reçu vérifié et attestations restent les gates de mutation.

L’impact GitNexus upstream est borné à Maintenance/Unit, sans processus affecté :
`RepairStore.apply_paths` est MEDIUM (14 symboles, 12 directs) et
`RepairStore.rollback_before_finalize` est LOW (3 symboles, 1 direct). Toute analyse HIGH ou
CRITICAL nouvelle hors de cette surface bloque l’implémentation.

La fonction reste non `SECURITY DEFINER` et conserve l’ACL PostgreSQL par défaut `PUBLIC EXECUTE`.
Une fonction `RETURNS trigger` n’est pas appelable comme une fonction SQL ordinaire. Le préflight
exige `current_user = proowner(public.update_updated_at())`, `nspowner(public)` inchangé, et aucune
default ACL de fonction applicable au rôle actif, globalement ou dans `public`; sinon il échoue avant
DDL. `CREATE FUNCTION` hérite donc ce même owner, sans `ALTER OWNER`.

La postcondition exige `proowner(nouvelle) = current_user = proowner(historique)`, `proacl IS NULL`,
et `COALESCE(proacl, acldefault('f', proowner)) = acldefault('f', proowner)`. L’ACL explosée prouve
exactement `EXECUTE` pour owner et grantee `0`/PUBLIC. `has_function_privilege(current_user,
function_oid, 'EXECUTE')` ne complète la preuve que pour le rôle de migration ; il ne prouve pas
PUBLIC. Aucun `GRANT` ni `REVOKE` n’est ajouté.

## Migration 039 fail-closed

### Upgrade

Les writers sont arrêtés. Dans une transaction unique, la migration prend d’abord :

```sql
LOCK TABLE public.project_contexts IN ACCESS EXCLUSIVE MODE;
```

Elle vérifie d’abord la fonction historique contre son canon migration 001, attributs et hash
`prosrc` inclus. Elle lit ensuite `pg_catalog` et exige une seule ligne de trigger avec `tgrelid` de
`public.project_contexts`, `tgfoid` de la fonction historique, `tgtype = 19` (row + before +
update), `tgattr = ''::int2vector` (aucun `UPDATE OF`), `tgqual IS NULL` (aucun `WHEN`),
`tgparentid = 0`, `tgconstraint = 0`, `tgconstrrelid = 0`, `tgconstrindid = 0`,
`tgdeferrable = false`, `tginitdeferred = false`, `tgoldtable IS NULL`, `tgnewtable IS NULL`,
`tgenabled = 'O'`, `tgisinternal = false`, `tgnargs = 0` et `tgargs = ''::bytea`. Toute divergence
échoue avant DDL. Cette liste exhaustive est le **contrat trigger exact** ; preflight, postflight,
downgrade, recovery v4 et tests l’appliquent sans la réduire.

Après création de la fonction dédiée, elle remappe le trigger avec ce DDL qualifié exact :

```sql
CREATE OR REPLACE TRIGGER trg_project_contexts_updated
BEFORE UPDATE ON public.project_contexts
FOR EACH ROW EXECUTE FUNCTION public.set_project_context_updated_at()
```

Elle relit exactement tous ces prédicats avec `tgfoid` de la fonction dédiée, puis fonction, owner
et ACL. Toute erreur annule la transaction : version Alembic, fonction et trigger restent inchangés.

### Downgrade

Le downgrade échoue sans DDL par défaut. Il exige explicitement :

```text
alembic -x allow_project_context_trigger_downgrade=yes downgrade 038
```

Cette option n’est autorisée qu’après rollback et ré-inventaire, ou après restauration PostgreSQL
complète. Avec l’opt-in, la migration prend le même verrou avant lecture catalogue et vérifie le
contrat trigger exact, la fonction historique contre son canon migration 001, ainsi que la fonction
dédiée et son owner/ACL. Elle rattache d’abord le
trigger à la fonction historique, relit ce contrat avec son `tgfoid`, puis exécute
`DROP FUNCTION public.set_project_context_updated_at()` sans `CASCADE`. Toute erreur annule
version, fonction et trigger vers leur état initial. Le postflight après `DROP FUNCTION` exige le
trigger historique exact, tous les prédicats de trigger ci-dessus, et l’absence de la fonction
dédiée.

## Recovery v4 et ordre opérateur

Les bytes et digests de recovery v3 sont immuables. L’autorité v4 exige `head = 039`,
`schema_version = 4` et 25 checks. Le 25e est `project_context_updated_at_039` ; il atteste la
fonction dédiée exacte, la fonction historique canonique, la condition GUC, le trigger exact, les
sept liaisons historiques `decisions`, `learnings`, `snippets`, `runbooks`, `adrs`, `features`,
`indexed_plans`, et les attributs/ACL.
v4 et ses tests comparent cet ensemble exact, jamais un simple compte de sept. Aucun résultat ne
revendique v4 avant un restore drill `pg_restore` à 25/25. Recovery v4 et les tests de migration
appliquent le même paquet de prédicats fonction/trigger que les preflight et postflight 039.

Les assets v4 sont `brain-v42-v4.json`, `brain-v42-v4.sql` et
`brain-v42-v4-pgrestore.sql`, avec leurs tests. Ils complètent v3 sans le modifier.

L’ordre de recovery est strict : `pg_restore` du backup production 037 en isolé, upgrade isolé
Dream 038 puis CAS 039, exécution de `brain-v42-v4-pgrestore.sql`, puis reçu isolé de type
`brain-v42-v4-pgrestore` à 25/25. Ce reçu isolé autorise le cutover, mais ne prouve pas encore la
production. Après writers off, l’opérateur upgrade la production en 038 puis 039, exécute
`brain-v42-v4.sql` live et obtient le reçu live autoritaire de type `brain-v42-v4-live` à 25/25.
Alors seulement il lance inventory/repair, puis rouvre les writers et redémarre le runtime.
`brain-v42-v4.sql` est donc l’attestation live, non une seconde restauration. Aucune preuve live
039 n’est revendiquée par cette livraison. Un backup 037 seul ne prouve jamais 039. Après un restore
post-finalize, l’opérateur réapplique 038 puis 039 et atteste v4 live avant writers ou runtime ; sinon il
reste explicitement en 037 avec l’ancien runtime.

Le head, store, runbook, spécification et plan passent à 039. `apply_paths` retourne
`already_applied` seulement si les sept contextes sont déjà dans l’état signé. Le rollback restaure
exactement chemins et timestamp non nul. Après finalisation, seule une restauration PostgreSQL
complète attestée restaure les données.

## Tests requis

Les preuves couvrent :

- statiques : fonction, trigger, owner, ACL, absence de `SECURITY DEFINER`, catalogue exact et
  hashes `prosrc` SHA-256 littéraux ;
- upgrade depuis 037 sans mutation de données, greenfield, downgrade sans opt-in atomique,
  downgrade opt-in, puis re-upgrade ;
- writer ordinaire avec timestamp explicite sans GUC écrasé par l’horloge serveur ;
- opt-in local qui préserve tout timestamp non nul, y compris égal à l’ancien, seulement dans apply
  et rollback, puis reset vérifié hors transaction ;
- GUC absent, `off`, `true`, `1` ou de casse différente qui force `CURRENT_TIMESTAMP` ;
- `NULL` sous GUC `on` rejeté atomiquement en `23502`, puis `NULL` sans GUC ou avec GUC invalide
  écrasé par `CURRENT_TIMESTAMP` avec UPDATE réussi ;
- invariance exacte de `decisions`, `learnings`, `snippets`, `runbooks`, `adrs`, `features` et
  `indexed_plans` ;
- fonction dédiée canonique, fonction historique migration 001 canonique, digests `prosrc` exacts
  et les huit mappings table/trigger/fonction exacts ;
- replay `apply_paths` en `already_applied`, rollback exact et CAS/finalize sans dérive ;
- migrations 038 puis 039, chaîne Alembic dans `tests/unit/test_alembic_env.py`, recovery v4 dans
  `tests/unit/test_recovery_contract_v4.py` et
  `tests/unit/test_recovery_contract_v4_pgrestore.py`, et suites repair 037→038→039 ;
- drift `BEFORE UPDATE OF` qui échoue et rollback totalement upgrade ou downgrade ;
- `RETURNING updated_at` exact, absence de GUC sur replay `already_*`, et reset après erreur de
  rollback dans `tests/unit/test_plan_index_repair_store.py` ;
- Task 7 : cinq preuves PostgreSQL sur cinq dans un conteneur jetable.

Les tests réexécutent la capture PostgreSQL 16 dans une base ou un conteneur au nom unique,
n’acceptent aucune URL de production et suppriment la ressource après capture. Les deux reçus v4,
isolé `brain-v42-v4-pgrestore` puis live `brain-v42-v4-live`, sont testés comme conditions
distinctes.

Les tests de migration utilisent une base isolée et capturent les valeurs complètes avant/après.

## Déploiement, rollback et risques

L’opérateur suit l’ordre de recovery ci-dessus, maintient les writers off jusqu’aux attestations,
puis exécute inventaire, `apply-paths`, reindex projet par projet, `verify` et `finalize`.
`install.sh` précède le restart, qui reste la dernière action.

| Risque | Parade |
| --- | --- |
| Trigger ou ACL inattendu | Verrou, préflight `pg_catalog` et vérification post-DDL. |
| Drift de fonction au même OID | Attributs et hash SHA-256 exact de `prosrc`. |
| Writer ordinaire qui injecte un timestamp ou `NULL` | GUC absent ou invalide : `CURRENT_TIMESTAMP` est forcé. |
| Timestamp de repair perdu | GUC local qui conserve tout timestamp non nul dans apply/rollback et CAS exact. |
| Cutover fondé sur un reçu isolé seul | Reçu live `brain-v42-v4-live` 25/25 obligatoire avant repair. |

## Fichiers prévus

- Migration : `alembic/versions/039_project_context_timestamp_cas.py`.
- Store : `src/brain_v42/maintenance/plan_index_repair_store.py` et
  `src/brain_v42/maintenance/plan_index_repair.py`.
- Tests repair : `tests/unit/test_plan_index_repair.py`,
  `tests/unit/test_plan_index_repair_store.py`, `tests/unit/test_repair_plan_index_cli.py` et
  `tests/integration/test_plan_index_repair.py`.
- Tests migration/intégration : `tests/unit/db/test_migration_039_project_context_timestamp.py`,
  `tests/integration/db/test_migration_039_project_context_timestamp.py` et
  `tests/unit/test_alembic_env.py`.
- CLI et documents de repair : `scripts/repair_plan_index.py`,
  `docs/PLAN_INDEX_REPAIR_RUNBOOK.md`, cette spécification, la spécification de repair et son plan.
- Documentation dépôt et production : `README.md`, `CLAUDE.md`, les documents `docs/` de runtime
  et le runbook opérateur.
- Recovery v4 : `ops/recovery/brain-v42-v4.json`, `ops/recovery/brain-v42-v4.sql`,
  `ops/recovery/brain-v42-v4-pgrestore.sql`, `tests/unit/test_recovery_contract_v4.py` et
  `tests/unit/test_recovery_contract_v4_pgrestore.py`.

Les artefacts historiques 037 et v3 ne changent pas.

## Critères d’acceptation

1. Le trigger conserve l’auto-timestamp ordinaire et conserve tout timestamp explicite non nul,
   même égal à l’ancien, seulement avec le GUC local de repair ; seul `NULL` sous GUC `on` échoue
   atomiquement et `NULL` sans GUC est auto-horodaté.
2. Apply, replay, rollback et finalize conservent un CAS exact sans dérive de timestamp.
3. Upgrade et downgrade verrouillent, vérifient le catalogue exact et annulent entièrement sur
   erreur.
4. `decisions`, `learnings`, `snippets`, `runbooks`, `adrs`, `features`, `indexed_plans`, les
   assets 037 et recovery v3 restent inchangés, avec leurs mappings historiques canoniques.
5. v4 n’est déclarée qu’après les 25 checks, dont `project_context_updated_at_039`, et le drill
   isolé `brain-v42-v4-pgrestore` 25/25, suivi du reçu live `brain-v42-v4-live` 25/25 avant repair.
