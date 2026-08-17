# Schéma de données — brain_v42

**État de livraison :** La cible du dépôt est 045. La révision 045 élargit `dream_runs.model` de
`varchar(30)` à `varchar(120)` : deux des cinq modèles de phase configurés n'y entraient pas, dont
le secours WET **déjà configuré** (`nvidia/nemotron-3-super-120b-a12b`, 33 car.), et un dépassement
faisait perdre la LIGNE entière — l'`INSERT` est best-effort. Elle a été appliquée en production le
16 août 2026 et **mesurée** à `045` juste après : colonne à 120, vue `codex_dream_run_v1` recréée
(elle bloque l'`ALTER` tant qu'elle projette la colonne) avec son `GRANT SELECT` à `codex_ro`
reposé, et 32 tables inchangées. La révision 038 ajoute les tentatives
d'extraction de tickets Dream, la révision 039 isole le trigger de timestamp de
`project_contexts`, la révision 040 ajoute `project_contexts.focus_updated_at`, la révision 041
sépare la provenance du contenu (`access_log.actor`, `access_count_human`,
`content_updated_at`), la révision 042 ajoute `dream_runs.project_key`, et la révision 043 date le STATUT de fraîcheur (`freshness_status_updated_at` + `freshness_source`) sur les six tables suivies par le decay. La 043 a été appliquée en production le 10 août 2026 et mesurée à `043` juste après, sans backfill — zéro ligne datée. La 042 a été appliquée le 8 août 2026, dream
arrêté, et la production **mesurée** à `042` juste après — 864 lignes conservées, toutes à
`project_key IS NULL`, et 32 tables inchangées. Aucune page de ce dépôt ne prouve un head déployé : Aucune page de ce dépôt ne prouve un head déployé :
`select version_num from alembic_version`. Cette ligne a affirmé 037 pendant trois jours après la
bascule.

## PostgreSQL + pgvector — Tables

### Extension pgvector

```sql
CREATE EXTENSION IF NOT EXISTS vector;
```

Le registre SQLAlchemy `METADATA` déclare 31 tables, dont les six tables de la fondation
graph ci-dessous. Un schéma neuf au head 045 contient 32 tables `public` en comptant
`alembic_version`, qui reste hors de `METADATA`. Les migrations 040 à 044 n'ajoutent que des
colonnes et la 045 n'en ajoute aucune — elle élargit une colonne existante — le compte est donc
inchangé depuis 038 : vérifié sur `brain`, mesuré à 32 juste après l'application de la 045. La migration 036 maintient
aussi dix vues `codex_*` au total : neuf nouvelles vues et `codex_brain_entity_v1`, créée en
024 puis remplacée en 036.

La migration 037 déclare `down_revision = "036"`. Le lifecycle v4 qu'elle porte tourne en
production depuis le 24 juillet 2026, après application séquentielle de 036 puis 037 et preuve
explicite avant le redémarrage MCP. La révision 045 est la tête du dépôt. La révision 038 ajoute
`ticket_extraction_attempts`, 039 isole le trigger de timestamp de `project_contexts`, 040
ajoute `focus_updated_at`, et 041 ajoute les colonnes de provenance — aucune des quatre n'ajoute
de table après 038. L'inventaire distingue
le schéma défini dans le dépôt de l'état déployé; tout nouvel environnement ou restore doit
prouver sa propre révision, et la production elle-même doit être mesurée, jamais recopiée.

## Fondation graph canonique (migrations 033–035)

La migration 033 crée cinq tables pour reconstruire le graphe depuis PostgreSQL. La
migration 034 ajoute `graph_projection_leases`, qui porte le leadership durable et la
génération de fencing du projecteur. La migration 035 étend ce singleton avec un interlock
de recovery reprenable. La fondation compte donc toujours six tables. Les tables métier
restent la source du contenu; `brain_entities` en projette l'identité et le cycle de vie,
`entity_relations` porte les faits relationnels, et `graph_outbox` alimente Neo4j.

L'installation du schéma n'active pas seule ce chemin. La production utilise le ledger
canonique avec `GRAPH_LEDGER_WRITE_ENABLED=true` depuis le cutover du 22 juillet; son head
Alembic a depuis avancé et se mesure, il ne se lit pas ici. Le restore et le rebuild graph au
head 035 restent des preuves historiques. Le run DR-v5 `20260724_150315` renouvelle le gate
PostgreSQL au head 037 avec 24/24
contrôles; il ne prouve ni le replay des rôles, propriétaires et ACL, ni un nouveau rebuild Neo4j,
ni la copie chiffrée off-host.

### Table `projects`

```sql
CREATE TABLE projects (
    project_key VARCHAR(50) PRIMARY KEY,
    display_name VARCHAR(200),
    registry_status VARCHAR(16) NOT NULL DEFAULT 'unclaimed'
        CHECK (registry_status IN ('claimed', 'unclaimed', 'archived')),
    source VARCHAR(16) NOT NULL DEFAULT 'reference'
        CHECK (source IN ('context', 'reference', 'manual')),
    metadata JSONB NOT NULL DEFAULT '{}',
    created_at TIMESTAMPTZ NOT NULL DEFAULT NOW(),
    updated_at TIMESTAMPTZ NOT NULL DEFAULT NOW(),
    CHECK (project_key ~ '^[a-z0-9]+([:-][a-z0-9]+)*$')
);
```

Un contexte projet crée une entrée `claimed`; une simple référence crée une entrée
`unclaimed`. La suppression du contexte repasse le projet en `unclaimed/reference`, efface
le `source_uuid` de son entité graph et conserve cette identité projectable.

### Table `project_aliases`

```sql
CREATE TABLE project_aliases (
    alias_key VARCHAR(128) PRIMARY KEY,
    project_key VARCHAR(50) NOT NULL
        REFERENCES projects(project_key) ON DELETE CASCADE,
    source VARCHAR(16) NOT NULL DEFAULT 'manual',
    created_at TIMESTAMPTZ NOT NULL DEFAULT NOW()
);

CREATE INDEX idx_project_aliases_project_key ON project_aliases(project_key);
```

La migration enregistre les alias historiques connus, par exemple `brain_v42` →
`brain-v42` et `auto_discord` → `auto-discord`. Elle normalise les colonnes projet et
`project_contexts.related_projects` pendant l'upgrade, puis des triggers appliquent la même
règle aux écritures suivantes. Le downgrade ne restaure pas les anciennes orthographes.

### Table `brain_entities`

```sql
CREATE TABLE brain_entities (
    id UUID PRIMARY KEY DEFAULT gen_random_uuid(),
    entity_type VARCHAR(32) NOT NULL,
    entity_key TEXT NOT NULL,
    source_uuid UUID,
    project_key VARCHAR(50) REFERENCES projects(project_key) ON DELETE RESTRICT,
    scope_kind VARCHAR(16) NOT NULL,
    display_label TEXT,
    lifecycle VARCHAR(16) NOT NULL DEFAULT 'active'
        CHECK (lifecycle IN ('active', 'archived', 'deleted')),
    revision BIGINT NOT NULL DEFAULT 1,
    metadata JSONB NOT NULL DEFAULT '{}',
    created_at TIMESTAMPTZ NOT NULL DEFAULT NOW(),
    updated_at TIMESTAMPTZ NOT NULL DEFAULT NOW(),
    deleted_at TIMESTAMPTZ,
    UNIQUE (entity_type, entity_key),
    CHECK (
        (scope_kind = 'global' AND project_key IS NULL)
        OR (scope_kind = 'project' AND project_key IS NOT NULL)
    )
);

CREATE UNIQUE INDEX uq_brain_entities_source_uuid
    ON brain_entities(source_uuid) WHERE source_uuid IS NOT NULL;
CREATE INDEX idx_brain_entities_project_lifecycle
    ON brain_entities(project_key, lifecycle);
CREATE INDEX idx_brain_entities_type_lifecycle
    ON brain_entities(entity_type, lifecycle);
```

Le registre couvre les Projects, les neuf Domains autorisés et les entités Decision,
Learning, Snippet, Runbook, ADR, Feature et Plan. Les UUID des lignes métier servent de
`source_uuid`; Project et Domain utilisent leur clé métier comme `entity_key`. Une entité
`archived` reste projetée pour préserver les lignées. Seul `deleted` demande sa suppression
dans Neo4j.

### Table `entity_relations`

```sql
CREATE TABLE entity_relations (
    id UUID PRIMARY KEY DEFAULT gen_random_uuid(),
    source_entity_id UUID NOT NULL
        REFERENCES brain_entities(id) ON DELETE RESTRICT,
    target_entity_id UUID NOT NULL
        REFERENCES brain_entities(id) ON DELETE RESTRICT,
    relation_type VARCHAR(32) NOT NULL CHECK (relation_type IN (
        'SUPERSEDES', 'MOTIVATED_BY', 'IMPLEMENTS', 'DOCUMENTS', 'USES',
        'RELATED_TO', 'CONTAINS', 'DEPENDS_ON', 'BELONGS_TO',
        'MERGED_INTO', 'BELONGS_TO_DOMAIN'
    )),
    origin VARCHAR(64) NOT NULL,
    origin_ref TEXT,
    confidence DOUBLE PRECISION CHECK (confidence >= 0.0 AND confidence <= 1.0),
    properties JSONB NOT NULL DEFAULT '{}',
    lifecycle VARCHAR(16) NOT NULL DEFAULT 'active'
        CHECK (lifecycle IN ('active', 'archived', 'deleted')),
    revision BIGINT NOT NULL DEFAULT 1,
    created_at TIMESTAMPTZ NOT NULL DEFAULT NOW(),
    updated_at TIMESTAMPTZ NOT NULL DEFAULT NOW(),
    deleted_at TIMESTAMPTZ,
    UNIQUE (source_entity_id, target_entity_id, relation_type),
    CHECK (source_entity_id <> target_entity_id)
);
```

Les relations `RELATED_TO` reçoivent une orientation stable afin de rester uniques. Les
propriétés projetables se limitent à `similarity`, `score`, `threshold`, `model`,
`model_version` et `method`; aucun contenu libre ni secret n'entre dans l'outbox. Une
modification matérielle incrémente `revision`.

### Table `graph_outbox`

```sql
CREATE TABLE graph_outbox (
    id BIGSERIAL PRIMARY KEY,
    event_id UUID NOT NULL DEFAULT gen_random_uuid() UNIQUE,
    entity_id UUID REFERENCES brain_entities(id) ON DELETE CASCADE,
    relation_id UUID REFERENCES entity_relations(id) ON DELETE CASCADE,
    aggregate_revision BIGINT NOT NULL,
    operation VARCHAR(16) NOT NULL CHECK (operation IN (
        'upsert_entity', 'delete_entity', 'upsert_relation', 'delete_relation'
    )),
    attempt_count INTEGER NOT NULL DEFAULT 0,
    available_at TIMESTAMPTZ NOT NULL DEFAULT NOW(),
    leased_until TIMESTAMPTZ,
    lease_owner VARCHAR(128),
    lease_generation BIGINT,
    claim_version BIGINT NOT NULL DEFAULT 0,
    delivered_at TIMESTAMPTZ,
    last_error_code VARCHAR(100),
    created_at TIMESTAMPTZ NOT NULL DEFAULT NOW(),
    UNIQUE (entity_id, aggregate_revision),
    UNIQUE (relation_id, aggregate_revision),
    CHECK (
        (entity_id IS NOT NULL AND relation_id IS NULL)
        OR (entity_id IS NULL AND relation_id IS NOT NULL)
    )
);

CREATE INDEX idx_graph_outbox_pending ON graph_outbox(available_at, id)
    WHERE delivered_at IS NULL;
```

Le projecteur revendique des lots avec `FOR UPDATE SKIP LOCKED` uniquement sous un
leadership PostgreSQL vivant et armé pour Neo4j. La sélection nominale empêche une révision
éligible de dépasser une révision antérieure encore pending. Chaque claim enregistre
`lease_owner` et `lease_generation`, incrémente `claim_version` et fixe `leased_until`.
Le renouvellement, l'acquittement et l'échec exigent le même owner, la même génération, la
même version de claim et des leases encore valides. Un échec relâche le lease, applique un
backoff exponentiel plafonné à 300 secondes et stocke un code borné. Après
`GRAPH_OUTBOX_MAX_ATTEMPTS`, `last_error_code='max_attempts'` et
`available_at='infinity'` isolent l'événement. Si une révision plus récente du même agrégat
est ensuite projetée avec succès, les anciennes révisions terminales sont acquittées avec
`last_error_code='superseded'`; elles ne maintiennent donc pas artificiellement le compteur
`exhausted` au-dessus de zéro. Si la limite configurée est abaissée, le prochain claim sous un
leader vivant normalise aussi les événements dont `attempt_count` dépasse déjà cette nouvelle
limite vers le même état terminal.

### Table `graph_projection_leases` (créée en 034, étendue en 035)

```sql
CREATE TABLE graph_projection_leases (
    slot VARCHAR(32) PRIMARY KEY,
    protocol_version INTEGER NOT NULL DEFAULT 2,
    generation BIGINT NOT NULL DEFAULT 0,
    owner VARCHAR(128),
    leased_until TIMESTAMPTZ,
    neo4j_armed_generation BIGINT,
    recovery_id UUID,
    recovery_phase VARCHAR(16) NOT NULL DEFAULT 'idle',
    last_completed_recovery_id UUID,
    updated_at TIMESTAMPTZ NOT NULL DEFAULT NOW(),
    CONSTRAINT graph_projection_leases_protocol_valid
        CHECK (protocol_version = 2),
    CONSTRAINT graph_projection_leases_armed_generation_valid
        CHECK (
            neo4j_armed_generation IS NULL
            OR neo4j_armed_generation = generation
        ),
    CONSTRAINT graph_projection_leases_recovery_state_valid
        CHECK (
            (
                recovery_id IS NULL
                AND recovery_phase = 'idle'
            )
            OR (
                recovery_id IS NOT NULL
                AND recovery_id IS DISTINCT FROM last_completed_recovery_id
                AND owner IS NOT NULL
                AND leased_until IS NOT NULL
                AND (
                    (
                        recovery_phase = 'prepared'
                        AND neo4j_armed_generation IS NULL
                    )
                    OR (
                        recovery_phase = 'neo_ready'
                        AND neo4j_armed_generation IS NOT NULL
                        AND neo4j_armed_generation = generation
                    )
                )
            )
        )
);
```

Le runtime utilise comme singleton la ligne `slot='neo4j'`; la clé primaire garantit son
unicité. La migration 034 l'initialise avec le protocole 2, la génération `0` et la
génération armée `0`. Elle ajoute aussi les deux colonnes de claim à `graph_outbox`, puis
efface `lease_owner`, `leased_until` et `lease_generation` sur les événements non livrés.
La migration 035 ajoute les trois colonnes de recovery et leur contrainte. Son downgrade les
retire seulement; le downgrade 034 supprime ensuite la table et les colonnes de claim, sans
restaurer les coordonnées de lease effacées par l'upgrade.

Le protocole v2 applique les invariants suivants :

- **Acquire.** Le même owner conserve sa génération et son état armé tant que son lease vit.
  Après expiration ou en l'absence d'owner, l'acquisition incrémente une génération armée,
  réutilise une génération non armée et démarre sans armement.
- **Arm.** Après l'activation durable de la génération dans Neo4j, PostgreSQL arme seulement
  le tuple exact `(owner, generation)` si son lease vit encore et si le protocole vaut `2`.
- **Claim.** Seul le tuple vivant et armé peut revendiquer un événement. Une autre génération
  peut reprendre immédiatement un ancien claim, même si son expiration est future, et
  incrémente alors `claim_version`. Renew, ACK et fail valident l'owner, la génération, la
  version du claim et les deux expirations; une validation périmée ne modifie pas l'événement.
- **Release.** PostgreSQL libère seulement le tuple vivant exact. Il efface `owner` et
  `leased_until`, mais conserve `neo4j_armed_generation`, afin que l'acquisition suivante
  incrémente la génération.

Le projecteur conserve normalement le tuple armé entre les polls; il appelle Release à son
arrêt ou après l'invalidation d'un fence, d'un claim ou d'un CAS. Son lease couvre au moins
deux intervalles de poll pour éviter un changement de génération dû à la seule configuration.

L'activation Neo4j normale exige un fence protocole 2 existant et sans `recovery_id`. Elle
accepte le tuple armé exact ou, pendant l'armement, la génération immédiatement précédente.
Elle ne crée jamais un fence manquant et ne franchit jamais un recovery marker actif.

Le recovery 035 applique les invariants suivants :

- **Prepare.** Sous verrou du singleton, une nouvelle recovery refuse un lease runtime vivant,
  incrémente `generation` une seule fois, renseigne `recovery_id`, entre en `prepared`,
  désarme la génération et requeue toutes les révisions canoniques dans la même transaction.
- **Interlock.** Tous les chemins runtime exigent `recovery_id IS NULL`. Une recovery active
  refuse un autre UUID, même après expiration. Le même UUID peut reprendre le lease expiré
  sans nouveau bump ni nouvelle requeue.
- **Neo4j reset.** Une transaction Neo4j supprime uniquement les labels de projection Brain et
  les `BrainProjectionCursor`, puis installe le tuple exact avec son recovery marker. Elle
  préserve le `BrainProjectionFence` et les nœuds sans label allowlisté. Elle accepte un fence
  absent, son marker exact sur une génération inférieure ou égale, ou un fence sans marker sur
  une génération inférieure. Pendant une reprise PostgreSQL `neo_ready`, elle accepte aussi le
  fence exact sans marker du même owner, cas du crash après finalisation Neo4j ; elle refuse tout
  marker étranger et toute génération finalisée plus récente.
- **Neo ready.** PostgreSQL passe en `neo_ready` et arme la génération par CAS exact seulement
  après le commit du reset Neo4j.
- **Resume neo_ready.** Le même UUID actif rejoue toujours le reset borné avant de finaliser.
  Le fence et les cursors survivants ne constituent pas une preuve d'intégrité du contenu. Un
  fence futur, un mauvais protocole ou un marker étranger reste refusé.
- **Finalize.** Neo4j retire d'abord le marker exact. PostgreSQL copie ensuite `recovery_id`
  dans `last_completed_recovery_id`, revient à `idle` et libère le lease. Cette colonne rend
  idempotente la dernière recovery terminée; elle ne constitue pas un journal historique.

La contrainte SQL exige un owner et un timestamp de lease pendant `prepared` et `neo_ready`;
les CAS du repository, et non le `CHECK`, imposent que ce lease soit encore vivant. Le reset
reste destructif malgré son allowlist : une base Neo4j dédiée reste obligatoire. L'option A ne
requiert pas de sauvegarde Neo4j, car PostgreSQL est l'unique état restauré et la projection est
reconstruite. Les confirmations CLI ne remplacent pas les preuves externes.

Les migrations 034–035 fournissent le fencing runtime et sa reprise opérateur. Elles
n'attestent ni un restore PostgreSQL au head exactement déployé, ni un rebuild Neo4j complet,
ni l'isolation des writers legacy.

### Backfill et triggers de la migration 033

L'upgrade verrouille les tables sources en `SHARE ROW EXCLUSIVE`, normalise les alias, puis
backfill les projets, entités, appartenances, supersessions et relations `MERGED_INTO`. Il
crée un événement initial pour chaque révision courante.

Les triggers maintiennent ensuite :

- le registre Project depuis `project_contexts`;
- les projets seulement référencés depuis `indexed_plan_chunks`, `gitlab_events`,
  `brain_sessions`, `search_log`, les tables de tickets et
  `project_contexts.related_projects`;
- les identités des sept tables `decisions`, `learnings`, `snippets`, `runbooks`, `adrs`,
  `features` et `indexed_plans`;
- les relations `BELONGS_TO`, `SUPERSEDES` et `MERGED_INTO` issues des colonnes métier;
- le cycle de vie des relations lorsque leurs extrémités changent;
- une instruction outbox dans la même transaction que chaque modification canonique.

Après migration, `project_contexts.project_key` est immuable. Renommer un projet exige une
opération de migration explicite; un `UPDATE` direct échoue.

### Table `decisions`

```sql
CREATE TABLE decisions (
    id UUID PRIMARY KEY DEFAULT gen_random_uuid(),
    title VARCHAR(200) NOT NULL,
    description TEXT NOT NULL,
    reasoning TEXT NOT NULL,
    alternatives TEXT[] DEFAULT '{}',
    consequences TEXT,
    project_key VARCHAR(50),
    tags TEXT[] DEFAULT '{}',
    status VARCHAR(20) NOT NULL DEFAULT 'active'
        CHECK (status IN ('active', 'superseded', 'deprecated')),
    superseded_by UUID REFERENCES decisions(id) ON DELETE SET NULL,
    embedding vector(1536),              -- pgvector type, Qodo-Embed-1-1.5B via GPU service
    metadata JSONB DEFAULT '{}',
    search_vector TSVECTOR,              -- colonne GENERATED ALWAYS AS ... STORED (créée par migration 001)
    created_at TIMESTAMPTZ NOT NULL DEFAULT NOW(),
    updated_at TIMESTAMPTZ NOT NULL DEFAULT NOW(),
    -- decay columns (migration 007)
    last_accessed_at TIMESTAMPTZ,
    access_count INTEGER DEFAULT 0,
    freshness_status VARCHAR(10) DEFAULT 'fresh',
    merged_into UUID                     -- set by brain_merge_entities
);

CREATE INDEX idx_decisions_search ON decisions USING GIN (search_vector);
CREATE INDEX idx_decisions_project ON decisions (project_key);
CREATE INDEX idx_decisions_status ON decisions (status);
CREATE INDEX idx_decisions_tags ON decisions USING GIN (tags);
CREATE INDEX idx_decisions_created ON decisions (created_at DESC);
CREATE INDEX idx_decisions_embedding ON decisions USING hnsw (embedding vector_cosine_ops)
    WITH (m = 16, ef_construction = 64);
```

### Table `learnings`

```sql
CREATE TABLE learnings (
    id UUID PRIMARY KEY DEFAULT gen_random_uuid(),
    topic VARCHAR(200) NOT NULL,
    insight TEXT NOT NULL,
    source TEXT,
    source_type VARCHAR(20) NOT NULL DEFAULT 'experience'
        CHECK (source_type IN ('documentation', 'experience', 'article', 'video', 'book',
                               'conversation', 'code_review', 'bug', 'external',
                               'research', 'automated')),
    confidence VARCHAR(10) NOT NULL DEFAULT 'medium'
        CHECK (confidence IN ('low', 'medium', 'high')),
    project_key VARCHAR(50),
    tags TEXT[] DEFAULT '{}',
    validated_at TIMESTAMPTZ,
    embedding vector(1536),
    metadata JSONB DEFAULT '{}',
    search_vector TSVECTOR,
    created_at TIMESTAMPTZ NOT NULL DEFAULT NOW(),
    updated_at TIMESTAMPTZ NOT NULL DEFAULT NOW(),
    -- decay columns (migration 007)
    last_accessed_at TIMESTAMPTZ,
    access_count INTEGER DEFAULT 0,
    freshness_status VARCHAR(10) DEFAULT 'fresh',
    merged_into UUID
);

CREATE INDEX idx_learnings_search ON learnings USING GIN (search_vector);
CREATE INDEX idx_learnings_project ON learnings (project_key);
CREATE INDEX idx_learnings_confidence ON learnings (confidence);
CREATE INDEX idx_learnings_tags ON learnings USING GIN (tags);
CREATE INDEX idx_learnings_created ON learnings (created_at DESC);
CREATE INDEX idx_learnings_embedding ON learnings USING hnsw (embedding vector_cosine_ops)
    WITH (m = 16, ef_construction = 64);
```

### Table `snippets`

```sql
CREATE TABLE snippets (
    id UUID PRIMARY KEY DEFAULT gen_random_uuid(),
    title VARCHAR(200) NOT NULL,
    intention TEXT NOT NULL,
    code TEXT NOT NULL,
    language VARCHAR(50) NOT NULL,
    dependencies TEXT[] DEFAULT '{}',
    usage_example TEXT,
    gotchas TEXT,
    project_key VARCHAR(50),
    tags TEXT[] DEFAULT '{}',
    use_count INTEGER NOT NULL DEFAULT 0,
    last_used_at TIMESTAMPTZ,
    embedding vector(1536),
    metadata JSONB DEFAULT '{}',
    search_vector TSVECTOR,
    created_at TIMESTAMPTZ NOT NULL DEFAULT NOW(),
    updated_at TIMESTAMPTZ NOT NULL DEFAULT NOW(),
    -- decay columns (migration 007)
    last_accessed_at TIMESTAMPTZ,
    access_count INTEGER DEFAULT 0,
    freshness_status VARCHAR(10) DEFAULT 'fresh',
    merged_into UUID
);

CREATE INDEX idx_snippets_search ON snippets USING GIN (search_vector);
CREATE INDEX idx_snippets_language ON snippets (language);
CREATE INDEX idx_snippets_project ON snippets (project_key);
CREATE INDEX idx_snippets_tags ON snippets USING GIN (tags);
CREATE INDEX idx_snippets_use_count ON snippets (use_count DESC);
CREATE INDEX idx_snippets_created ON snippets (created_at DESC);
CREATE INDEX idx_snippets_embedding ON snippets USING hnsw (embedding vector_cosine_ops)
    WITH (m = 16, ef_construction = 64);
```

### Table `runbooks`

```sql
CREATE TABLE runbooks (
    id UUID PRIMARY KEY DEFAULT gen_random_uuid(),
    title VARCHAR(200) NOT NULL,
    description TEXT NOT NULL,
    project_key VARCHAR(50) NOT NULL,
    trigger TEXT NOT NULL,
    prerequisites TEXT[] DEFAULT '{}',
    steps JSONB NOT NULL DEFAULT '[]',          -- List[RunbookStep] sérialisé
    rollback_steps JSONB DEFAULT '[]',          -- List[RunbookStep] sérialisé
    estimated_duration VARCHAR(50),
    tags TEXT[] DEFAULT '{}',
    execution_count INTEGER NOT NULL DEFAULT 0,
    last_executed_at TIMESTAMPTZ,
    last_execution_status VARCHAR(20)
        CHECK (last_execution_status IN ('success', 'failed', 'partial', 'skipped')),
    embedding vector(1536),
    metadata JSONB DEFAULT '{}',
    search_vector TSVECTOR,
    created_at TIMESTAMPTZ NOT NULL DEFAULT NOW(),
    updated_at TIMESTAMPTZ NOT NULL DEFAULT NOW(),
    -- decay columns (migration 007)
    last_accessed_at TIMESTAMPTZ,
    access_count INTEGER DEFAULT 0,
    freshness_status VARCHAR(10) DEFAULT 'fresh',
    merged_into UUID,

    UNIQUE(title, project_key)
);

CREATE INDEX idx_runbooks_search ON runbooks USING GIN (search_vector);
CREATE INDEX idx_runbooks_project ON runbooks (project_key);
CREATE INDEX idx_runbooks_tags ON runbooks USING GIN (tags);
CREATE INDEX idx_runbooks_created ON runbooks (created_at DESC);
CREATE INDEX idx_runbooks_embedding ON runbooks USING hnsw (embedding vector_cosine_ops)
    WITH (m = 16, ef_construction = 64);
```

### Table `adrs`

```sql
CREATE TABLE adrs (
    id UUID PRIMARY KEY DEFAULT gen_random_uuid(),
    number INTEGER NOT NULL,
    title VARCHAR(200) NOT NULL,
    context TEXT NOT NULL,
    decision TEXT NOT NULL,
    consequences TEXT NOT NULL,
    alternatives_considered JSONB DEFAULT '[]',  -- List[AlternativeConsidered]
    project_key VARCHAR(50) NOT NULL,
    tags TEXT[] DEFAULT '{}',
    status VARCHAR(20) NOT NULL DEFAULT 'proposed'
        CHECK (status IN ('proposed', 'accepted', 'deprecated', 'superseded')),
    decided_at TIMESTAMPTZ,
    superseded_by INTEGER,                       -- ADR number (pas UUID)
    embedding vector(1536),
    metadata JSONB DEFAULT '{}',
    search_vector TSVECTOR,
    created_at TIMESTAMPTZ NOT NULL DEFAULT NOW(),
    updated_at TIMESTAMPTZ NOT NULL DEFAULT NOW(),
    -- decay columns (migration 007)
    last_accessed_at TIMESTAMPTZ,
    access_count INTEGER DEFAULT 0,
    freshness_status VARCHAR(10) DEFAULT 'fresh',
    merged_into UUID,

    UNIQUE(number, project_key)
);

CREATE INDEX idx_adrs_search ON adrs USING GIN (search_vector);
CREATE INDEX idx_adrs_project ON adrs (project_key);
CREATE INDEX idx_adrs_status ON adrs (status);
CREATE INDEX idx_adrs_tags ON adrs USING GIN (tags);
CREATE INDEX idx_adrs_number ON adrs (project_key, number DESC);
CREATE INDEX idx_adrs_created ON adrs (created_at DESC);
CREATE INDEX idx_adrs_embedding ON adrs USING hnsw (embedding vector_cosine_ops)
    WITH (m = 16, ef_construction = 64);
```

### Table `project_contexts`

```sql
CREATE TABLE project_contexts (
    id UUID PRIMARY KEY DEFAULT gen_random_uuid(),
    project_key VARCHAR(50) NOT NULL UNIQUE,
    name VARCHAR(200) NOT NULL,
    description TEXT NOT NULL,
    languages TEXT[] DEFAULT '{}',
    frameworks TEXT[] DEFAULT '{}',
    databases TEXT[] DEFAULT '{}',
    code_style TEXT,
    git_workflow TEXT,
    test_strategy TEXT,
    current_phase TEXT,
    current_focus TEXT,
    focus_revision BIGINT NOT NULL DEFAULT 0, -- migration 032; CAS for session end
    focus_updated_at TIMESTAMPTZ,             -- migration 040; NULL = jamais mesuré
    blockers TEXT[] DEFAULT '{}',
    related_projects TEXT[] DEFAULT '{}',
    local_path TEXT,
    repo_url TEXT,
    decisions_count INTEGER NOT NULL DEFAULT 0,
    learnings_count INTEGER NOT NULL DEFAULT 0,
    snippets_count INTEGER NOT NULL DEFAULT 0,
    runbooks_count INTEGER NOT NULL DEFAULT 0,
    adrs_count INTEGER NOT NULL DEFAULT 0,
    metadata JSONB DEFAULT '{}',
    plan_scan_paths TEXT[] DEFAULT '{}',         -- drives PlanIndexer
    gitlab_project_path VARCHAR(200),            -- drives webhook ingestion
    project_group VARCHAR(50),                   -- for cross-project search
    created_at TIMESTAMPTZ NOT NULL DEFAULT NOW(),
    updated_at TIMESTAMPTZ NOT NULL DEFAULT NOW()
);

CREATE INDEX idx_project_contexts_key ON project_contexts (project_key);
CREATE INDEX idx_project_contexts_languages ON project_contexts USING GIN (languages);
CREATE INDEX idx_project_contexts_frameworks ON project_contexts USING GIN (frameworks);
```

**Note**: Pas d'embedding ni de search_vector pour ProjectContext (pas de recherche sémantique sur les projets).

La migration 032 ajoute aussi un trigger `project_contexts_focus_revision_trigger`.
Avant chaque mise à jour de `current_focus`, il incrémente `focus_revision` uniquement
si la valeur du focus change.

La migration 040 ajoute `focus_updated_at`, qui date la prose du focus. `updated_at` ne peut pas
le faire : il bouge à chaque écriture de la ligne, compteurs inclus. La colonne est écrite par le
code applicatif via `brain_v42.db.focus_stamp`, jamais par un trigger, et sous la même condition
`IS DISTINCT FROM` que `focus_revision` — réécrire le focus à l'identique ne la rajeunit pas,
ce qui est précisément ce qui rend un recopiage visible. Aucun backfill : `NULL` signifie
« jamais mesuré » et se répare à la première vraie écriture de focus.

La migration 033 normalise `project_key` et `related_projects` via `project_aliases`, puis
interdit le changement direct de `project_key`. Elle synchronise aussi le Project canonique,
son entité graph et leur événement outbox.

### Tables de session (migrations 032 et 037)

```sql
CREATE TABLE brain_sessions (
    id UUID PRIMARY KEY DEFAULT gen_random_uuid(),
    project_key VARCHAR(50) NOT NULL
        REFERENCES project_contexts(project_key) ON DELETE RESTRICT,
    client_key VARCHAR(128) NOT NULL CHECK (btrim(client_key) <> ''),
    status VARCHAR(20) NOT NULL DEFAULT 'open'
        CHECK (status IN ('open', 'ended', 'abandoned')),
    started_focus TEXT,
    started_focus_revision BIGINT NOT NULL,
    summary TEXT,
    next_focus TEXT,
    captured_knowledge_ids UUID[] NOT NULL DEFAULT '{}',
    nothing_to_capture_reason TEXT,
    abandonment_reason TEXT,
    end_expected_focus_revision BIGINT,
    focus_outcome VARCHAR(20),
    focus_at_end TEXT,
    focus_revision_at_end BIGINT,
    started_at TIMESTAMPTZ NOT NULL DEFAULT NOW(),
    last_heartbeat_at TIMESTAMPTZ NOT NULL DEFAULT NOW(),
    ended_at TIMESTAMPTZ,
    updated_at TIMESTAMPTZ NOT NULL DEFAULT NOW(),
    CONSTRAINT uq_brain_sessions_project_client
        UNIQUE (project_key, client_key),
    CONSTRAINT brain_sessions_focus_outcome_valid CHECK (
        focus_outcome IS NULL OR focus_outcome IN ('applied', 'conflict')
    ),
    CONSTRAINT brain_sessions_capture_ids_valid CHECK (
        cardinality(captured_knowledge_ids) <= 100
        AND array_position(captured_knowledge_ids, NULL) IS NULL
    ),
    CONSTRAINT brain_sessions_terminal_state_valid CHECK (
        (status = 'open'
            AND ended_at IS NULL
            AND summary IS NULL
            AND next_focus IS NULL
            AND cardinality(captured_knowledge_ids) = 0
            AND nothing_to_capture_reason IS NULL
            AND abandonment_reason IS NULL
            AND end_expected_focus_revision IS NULL
            AND focus_outcome IS NULL
            AND focus_at_end IS NULL
            AND focus_revision_at_end IS NULL)
        OR
        (status = 'ended'
            AND ended_at IS NOT NULL
            AND summary IS NOT NULL AND btrim(summary) <> ''
            AND next_focus IS NOT NULL AND btrim(next_focus) <> ''
            AND abandonment_reason IS NULL
            AND focus_outcome IS NOT NULL
            AND (end_expected_focus_revision IS NULL
                OR end_expected_focus_revision >= 0)
            AND (focus_revision_at_end IS NULL
                OR focus_revision_at_end >= 0)
            AND ((end_expected_focus_revision IS NULL
                    AND focus_outcome = 'applied'
                    AND focus_at_end = next_focus
                    AND focus_revision_at_end IS NULL)
                OR (end_expected_focus_revision IS NOT NULL
                    AND focus_revision_at_end IS NOT NULL
                    AND ((focus_outcome = 'applied'
                            AND focus_at_end = next_focus
                            AND focus_revision_at_end =
                                end_expected_focus_revision + 1)
                        OR (focus_outcome = 'conflict'
                            AND focus_revision_at_end <>
                                end_expected_focus_revision))))
            AND ((cardinality(captured_knowledge_ids) > 0
                    AND nothing_to_capture_reason IS NULL)
                OR (cardinality(captured_knowledge_ids) = 0
                    AND nothing_to_capture_reason IS NOT NULL
                    AND btrim(nothing_to_capture_reason) <> '')))
        OR
        (status = 'abandoned'
            AND ended_at IS NOT NULL
            AND summary IS NULL
            AND next_focus IS NULL
            AND cardinality(captured_knowledge_ids) = 0
            AND nothing_to_capture_reason IS NULL
            AND abandonment_reason IS NOT NULL
            AND btrim(abandonment_reason) <> ''
            AND end_expected_focus_revision IS NULL
            AND focus_outcome IS NULL
            AND focus_at_end IS NULL
            AND focus_revision_at_end IS NULL)
    )
);

CREATE INDEX idx_brain_sessions_project_status_started
    ON brain_sessions (project_key, status, started_at DESC);

CREATE TABLE brain_session_artifacts (
    knowledge_id UUID PRIMARY KEY,
    session_id UUID NOT NULL
        REFERENCES brain_sessions(id) ON DELETE CASCADE,
    knowledge_type VARCHAR(32) NOT NULL
        CHECK (knowledge_type IN (
            'decision', 'learning', 'snippet', 'runbook',
            'adr', 'indexed_plan', 'legacy'
        )),
    captured_at TIMESTAMPTZ NOT NULL DEFAULT NOW()
);

CREATE INDEX idx_brain_session_artifacts_session_captured
    ON brain_session_artifacts (session_id, captured_at);
```

Plusieurs sessions `open` peuvent coexister pour un même projet. La paire
`(project_key, client_key)` fournit l'idempotence du démarrage; aucune contrainte
d'unicité ne limite le nombre de sessions ouvertes. Les mutations ciblées exigent aussi
`expected_client_key` au niveau applicatif. La comparaison avec le `client_key` stocké
protège contre un UUID de session erroné, mais ne constitue pas une authentification.

`brain_session_artifacts` porte la provenance persistante déclarée par le client. La clé
primaire `knowledge_id` attribue chaque UUID à une seule session. Le repository verrouille
le projet, puis vérifie que chaque UUID existe dans `decisions`, `learnings`, `snippets`,
`runbooks`, `adrs` ou `indexed_plans`, appartient au même projet et a été créé depuis
`started_at`. Cette attribution prouve la déclaration persistée, pas l'identité du processus
qui a créé l'artefact. `captured_knowledge_ids` reste vide pendant la session et reçoit une
copie du ledger à la fin comme snapshot terminal.

Le modèle/API expose séparément `attributed_knowledge_ids`, champ dérivé qui n'est pas une
colonne. Le repository le réhydrate depuis le ledger pour les retries de démarrage, les
lectures, listes, reprises, captures, heartbeats et abandons. Une session abandonnée garde
donc une vue et la propriété exclusive de ses attributions, même si son snapshot terminal
`captured_knowledge_ids` doit rester vide.

`last_heartbeat_at` sert au calcul applicatif de `is_stale`. Une session `open` est stale
après 24 heures sans heartbeat; ce statut est dérivé, n'est pas stocké et ne ferme jamais la
session automatiquement. Un heartbeat ou une capture actualise l'horodatage.

La fin exige soit un ledger non vide, soit `nothing_to_capture_reason`, exclusivement. Elle
persiste la tentative de focus dans `end_expected_focus_revision`, puis son résultat dans
`focus_outcome`, `focus_at_end` et `focus_revision_at_end`. Si la révision attendue correspond,
le focus est appliqué et la révision avance (`applied`). Sinon, le focus partagé reste inchangé,
mais la session finit quand même (`conflict`). Ces champs rendent le replay terminal stable.

Lors de l'upgrade 037, `last_heartbeat_at` reprend `updated_at`. Les sessions v3 terminées
reçoivent `focus_outcome='applied'` et `focus_at_end=next_focus`; la révision demandée et la
révision résultante restent `NULL`, car elles n'étaient pas persistées. Les captures
terminales v3 sont copiées avec `knowledge_type='legacy'`. L'upgrade échoue si un même UUID
apparaît dans plusieurs sessions, afin de ne pas inventer une provenance; les doublons d'un
même UUID dans une seule session v3 sont dédupliqués dans le ledger.

Le downgrade 037→036 refuse toute attribution qui n'est pas déjà reflétée dans le snapshot
d'une session `ended`, ainsi que tout `focus_outcome='conflict'`. Ces états sont valides en
v4 mais non représentables en v3; le rollback doit donc être préparé hors ligne plutôt que
de supprimer silencieusement leur provenance ou leur outcome.

### Table `indexed_plans` (migration 009 + étendue par migration 014)

```sql
-- Colonnes de base (migration 009)
CREATE TABLE indexed_plans (
    id UUID PRIMARY KEY DEFAULT gen_random_uuid(),
    file_path VARCHAR(500) NOT NULL UNIQUE,
    title VARCHAR(200) NOT NULL,   -- VARCHAR(200) en DB (migration 009) ; tables.py déclare String(500) — drift connu
    plan_type VARCHAR(20) NOT NULL,          -- 'spec' | 'plan'
    project_key VARCHAR(50) NOT NULL,
    content_hash VARCHAR(64) NOT NULL,       -- skip unchanged files on reindex
    embedding vector(1536),
    created_at TIMESTAMPTZ NOT NULL DEFAULT NOW(),
    updated_at TIMESTAMPTZ NOT NULL DEFAULT NOW()
);

-- Colonnes ajoutées par migration 014 (plan_chunks)
ALTER TABLE indexed_plans ADD COLUMN content TEXT NOT NULL DEFAULT '';
ALTER TABLE indexed_plans ADD COLUMN summary TEXT;
ALTER TABLE indexed_plans ADD COLUMN search_vector TSVECTOR;
ALTER TABLE indexed_plans ADD COLUMN tags VARCHAR[] NOT NULL DEFAULT '{}';
ALTER TABLE indexed_plans ADD COLUMN metadata JSONB NOT NULL DEFAULT '{}';
ALTER TABLE indexed_plans ADD COLUMN status VARCHAR(20) NOT NULL DEFAULT 'active'
    CHECK (status IN ('draft', 'active', 'archived'));
ALTER TABLE indexed_plans ADD COLUMN chunk_count INTEGER NOT NULL DEFAULT 0;
ALTER TABLE indexed_plans ADD COLUMN word_count INTEGER NOT NULL DEFAULT 0;
ALTER TABLE indexed_plans ADD COLUMN access_count INTEGER NOT NULL DEFAULT 0;
ALTER TABLE indexed_plans ADD COLUMN last_accessed_at TIMESTAMPTZ;
ALTER TABLE indexed_plans ADD COLUMN freshness_status VARCHAR(20) NOT NULL DEFAULT 'fresh'
    CHECK (freshness_status IN ('fresh', 'stale', 'archived'));
ALTER TABLE indexed_plans ADD COLUMN indexed_at TIMESTAMPTZ NOT NULL DEFAULT NOW();

-- Indexes ajoutés par migration 014
CREATE INDEX idx_indexed_plans_tags ON indexed_plans USING GIN(tags);
CREATE INDEX idx_indexed_plans_search_vector ON indexed_plans USING GIN(search_vector);
CREATE INDEX idx_indexed_plans_pk_status_fresh ON indexed_plans(project_key, status, freshness_status);

-- Index ajouté par migration 027
CREATE INDEX idx_indexed_plans_updated_at ON indexed_plans (updated_at DESC);
-- Eliminates filesort on list_plans() ORDER BY updated_at DESC
```

### Table `indexed_plan_chunks` (migration 014)

Créée par `014_plan_chunks.py` via raw SQL. Déclarée dans `tables.py` pour le support autogenerate d'Alembic.

```sql
CREATE TABLE indexed_plan_chunks (
    id UUID PRIMARY KEY DEFAULT gen_random_uuid(),
    plan_id UUID NOT NULL REFERENCES indexed_plans(id) ON DELETE CASCADE,
    section_title VARCHAR(500) NOT NULL,
    section_path VARCHAR(1000) NOT NULL,
    content TEXT NOT NULL,
    section_order INTEGER NOT NULL,
    word_count INTEGER NOT NULL DEFAULT 0,
    embedding VECTOR(1536) NOT NULL,         -- required (non-nullable)
    search_vector TSVECTOR,
    tags VARCHAR[] NOT NULL DEFAULT '{}',
    project_key VARCHAR(50) NOT NULL,
    plan_type VARCHAR(20) NOT NULL
        CHECK (plan_type IN ('spec', 'plan')),
    status VARCHAR(20) NOT NULL DEFAULT 'active'
        CHECK (status IN ('draft', 'active', 'archived')),
    access_count INTEGER NOT NULL DEFAULT 0,
    last_accessed_at TIMESTAMPTZ,
    created_at TIMESTAMPTZ NOT NULL DEFAULT NOW()
);

CREATE INDEX idx_plan_chunks_plan_id ON indexed_plan_chunks(plan_id);
CREATE INDEX idx_plan_chunks_embedding ON indexed_plan_chunks
    USING hnsw (embedding vector_cosine_ops) WITH (m = 16, ef_construction = 64);
CREATE INDEX idx_plan_chunks_tags ON indexed_plan_chunks USING GIN(tags);
CREATE INDEX idx_plan_chunks_search_vector ON indexed_plan_chunks USING GIN(search_vector);
CREATE INDEX idx_plan_chunks_pk_type ON indexed_plan_chunks(project_key, plan_type);
```

**Note**: `plan_id` est la FK vers `indexed_plans`. Le `id` du chunk n'est pas exposé via MCP — `brain_get(entity_type="plan", entity_id=...)` prend le `plan_id` (UUID du plan parent).

---

## Famille coordination — Tickets cross-projet (migrations 028–029 et 038)

> Ces 4 tables sont **hors** famille mémoire : pas d'embedding, pas de `search_vector`, pas de colonnes decay (`freshness_status`, `last_accessed_at`, `access_count`), pas de sync Neo4j. Les tickets sont du transient adressé — seule la table `ticket_extraction_proposals` constitue le pont vers la famille mémoire (via `scripts/ticket_extract.py`).

### Table `tickets` (migration 028)

```sql
CREATE TABLE tickets (
    id UUID PRIMARY KEY DEFAULT gen_random_uuid(),
    kind VARCHAR(10) NOT NULL
        CONSTRAINT tickets_kind_valid CHECK (kind IN ('request', 'fyi')),
    title VARCHAR(200) NOT NULL,
    body TEXT NOT NULL,
    from_project VARCHAR(50) NOT NULL,       -- émetteur (kebab-case canonique)
    to_project VARCHAR(50) NOT NULL,         -- destinataire
    status VARCHAR(15) NOT NULL DEFAULT 'open'
        CONSTRAINT tickets_status_valid
            CHECK (status IN ('open', 'in_progress', 'resolved', 'wontfix', 'closed', 'acked')),
    extraction_status VARCHAR(10)            -- NULL jusqu'à clôture, puis 'pending'
        CONSTRAINT tickets_extraction_status_valid
            CHECK (extraction_status IS NULL
                   OR extraction_status IN ('pending', 'proposed', 'skipped', 'done')),
    resolved_at TIMESTAMPTZ,
    closed_at TIMESTAMPTZ,
    created_at TIMESTAMPTZ NOT NULL DEFAULT NOW(),
    updated_at TIMESTAMPTZ NOT NULL DEFAULT NOW()
    -- PAS d'embedding, PAS de search_vector, PAS de colonnes decay
);

CREATE INDEX idx_tickets_to_project_status ON tickets (to_project, status);
CREATE INDEX idx_tickets_from_project_status ON tickets (from_project, status);
CREATE INDEX idx_tickets_extraction_pending ON tickets (extraction_status)
    WHERE extraction_status = 'pending';    -- index partiel — seuls les tickets à extraire
```

### Table `ticket_messages` (migration 028)

```sql
CREATE TABLE ticket_messages (
    id UUID PRIMARY KEY DEFAULT gen_random_uuid(),
    ticket_id UUID NOT NULL REFERENCES tickets(id) ON DELETE CASCADE,
    author_project VARCHAR(50) NOT NULL,    -- kebab-case canonique
    body TEXT NOT NULL,
    status_to VARCHAR(15),                  -- non-NULL si le message accompagne une transition
    created_at TIMESTAMPTZ NOT NULL DEFAULT NOW()
    -- PAS d'updated_at : les messages sont immuables
);

CREATE INDEX idx_ticket_messages_ticket ON ticket_messages (ticket_id, created_at);
```

### Table `ticket_extraction_proposals` (migration 029)

Pont entre la famille coordination et la famille mémoire. Générée par `scripts/ticket_extract.py` (pattern proposer-only, review humaine avant apply).

```sql
CREATE TABLE ticket_extraction_proposals (
    id BIGSERIAL PRIMARY KEY,               -- séquentiel (pas UUID) — ordre d'insertion
    ticket_id UUID REFERENCES tickets(id) ON DELETE SET NULL,   -- nullable (ticket supprimé)
    target_type VARCHAR(10) NOT NULL
        CONSTRAINT tep_target_type_valid CHECK (target_type IN ('learning', 'decision')),
    target_project VARCHAR(50) NOT NULL,    -- projet cible de l'entité créée
    payload JSONB NOT NULL,                 -- champs de la future entité (titre, contenu, etc.)
    rationale TEXT,                         -- explication LLM du choix d'extraire
    status VARCHAR(10) NOT NULL DEFAULT 'proposed'
        CONSTRAINT tep_status_valid CHECK (status IN ('proposed', 'applied', 'rejected')),
    applied_entity_id UUID,                 -- UUID de l'entité créée après apply
    created_at TIMESTAMPTZ NOT NULL DEFAULT NOW(),
    applied_at TIMESTAMPTZ                  -- timestamp de l'apply
);

CREATE INDEX idx_tep_status ON ticket_extraction_proposals (status);
CREATE INDEX idx_tep_ticket ON ticket_extraction_proposals (ticket_id);
```

### Table `ticket_extraction_attempts` (migration 038)

Journal terminal des tentatives Dream EXTRACT. Une tentative interrompue ou différée reste
observable sans créer d'état de lease persistant; le ticket peut être repris au run suivant.

```sql
CREATE TABLE ticket_extraction_attempts (
    id BIGSERIAL PRIMARY KEY,
    ticket_id UUID NOT NULL REFERENCES tickets(id) ON DELETE CASCADE,
    run_date DATE NOT NULL,
    status VARCHAR(10) NOT NULL
        CONSTRAINT ticket_extraction_attempts_status_valid
            CHECK (status IN ('done', 'failed', 'timeout', 'deferred')),
    duration_s DOUBLE PRECISION NOT NULL,
    error_message TEXT,
    created_at TIMESTAMPTZ NOT NULL DEFAULT NOW()
);

CREATE INDEX idx_ticket_extraction_attempts_ticket
    ON ticket_extraction_attempts (ticket_id, created_at);
CREATE INDEX idx_ticket_extraction_attempts_date
    ON ticket_extraction_attempts (run_date, status);
```

**Cycle extraction :**
1. Ticket atteint `closed` ou `acked` → `extraction_status = 'pending'`
2. `ticket_extract.py` compare chaque draft, par embedding canonique, aux learnings/décisions actifs du même projet et aux drafts déjà retenus dans le run. La recherche corpus est exacte ; cosine `>= 0,85` → draft supprimée.
3. Une ligne active du projet avec embedding absent ou non comparable (norme `<= 1e-6`), un nouveau vecteur invalide ou une panne DB/embedding fait échouer la gate entière : aucune persistance, aucun apply WET et tickets laissés `pending`.
4. Sous verrou `FOR UPDATE`, un ticket encore `pending` est revendiqué et les drafts nouvelles deviennent `status = 'proposed'`; `tickets.extraction_status` passe à `'proposed'`. Un runner concurrent devenu obsolète ne persiste rien.
5. Review humaine : apply via `python -m scripts.ticket_extract --apply-ids "<ids>"` → `status = 'applied'`, `applied_entity_id` renseigné, `tickets.extraction_status = 'done'`. Cet override opérateur ne rejoue pas la gate automatique.
6. Sans proposal retenue après déduplication → `tickets.extraction_status = 'skipped'`.

Le seuil `ticket_extract_corpus_dedup_cosine=0,85` est inventorié mais non calibré. EXTRACT reste en DRY jusqu'à plusieurs nuits de soak, revue humaine du taux de doublons et mesure du coût de la recherche exacte.

---

### Autres tables

| Table | Migration | Rôle |
|-------|-----------|------|
| `features` | 005/009/030 | Roadmap tracking — statuts planned/research/design/building/deployed/done/archived |
| `feature_artifacts` | 005 | Liens feature ↔ artifact (décision, learning, gitlab event, etc.) |
| `search_log` | 004 | 1 ligne par appel brain_search — qualité des recherches (30j rétention) |
| `process_metrics` | 004 | Snapshot des compteurs in-memory par process MCP (upsert 30s) |
| `access_log` | 006 | Lectures enregistrées par AccessLogger → décroissance de fraîcheur |
| `consolidation_log` | 008 | Audit des merges (brain_merge_entities) — 1 ligne par merge |
| `gitlab_events` | 009/019 | Raw webhook payloads GitLab (dédupliqués sur gitlab_event_id) |
| `dream_runs` | 013/015/022 | 1 ligne par phase Dream (SCAN/CLEAN/CONNECT/SYNTH/REORG/EXTRACT) |
| `dream_promotions` | 016/017/021 | Audit des promotions learning → ADR/runbook par Dream SYNTH |
| `metrics_timeseries` | 018 | Historique 24h pour le cockpit red-monitor (bucket_ts + metric PK) |
| `tickets` | 028 | Demandes et FYI adressés entre projets |
| `ticket_messages` | 028 | Historique des messages associés aux tickets |
| `ticket_extraction_proposals` | 029 | Propositions de capitalisation issues des tickets terminaux |
| `ticket_extraction_attempts` | 038 | Journal terminal des tentatives Dream EXTRACT |
| `roadmap_curation_proposals` | 030/031 | Propositions de curation roadmap et journal JSONB d'application |
| `brain_sessions` | 032/037 | Cycle explicite, persistant et concurrent des sessions Brain |
| `brain_session_artifacts` | 037 | Ledger exclusif des artefacts attribués aux sessions |

Index notable ajouté par migration 027 sur `consolidation_log`:
```sql
CREATE INDEX idx_consolidation_log_entity_type ON consolidation_log (entity_type);
-- get_handled_pairs() WHERE entity_type = ? — évite le seq-scan sur cette table sans index
```

## Trigger updated_at automatique

```sql
CREATE OR REPLACE FUNCTION update_updated_at()
RETURNS TRIGGER AS $$
BEGIN
    NEW.updated_at = NOW();
    RETURN NEW;
END;
$$ LANGUAGE plpgsql;
-- Triggers on: decisions, learnings, snippets, runbooks, adrs,
-- project_contexts, features, indexed_plans
```

## Migrations Alembic

45 révisions (001 → 045), dans `alembic/versions/`.

| Révision | Contenu principal |
|----------|-------------------|
| 001 | Schéma initial (6 tables knowledge + triggers) |
| 002 | Embedding 384 → 1536 dims |
| 003 | Correction du CHECK source_type |
| 004 | search_log + process_metrics |
| 005 | features + feature_artifacts (roadmap) |
| 006 | access_log |
| 007 | Colonnes decay (last_accessed_at, access_count, freshness_status) |
| 008 | consolidation_log |
| 009 | roadmap_v2 : CREATE indexed_plans + gitlab_events, features.pinned, project_contexts.plan_scan_paths/gitlab_project_path |
| 010 | superseded_by ON DELETE SET NULL |
| 011 | Corrections de cohérence de schéma |
| 012 | project_groups + normalisation project_key |
| 013 | dream_runs |
| 014 | indexed_plan_chunks + extension indexed_plans (content, tags, search_vector, decay) + 3 index |
| 015 | dream_runs.error_message |
| 016 | dream_promotions |
| 017 | dream_promotions tombstone |
| 018 | metrics_timeseries |
| 019 | gitlab_events.feature_id ON DELETE SET NULL |
| 020 | Source type 'automated' |
| 021 | dream_promotions.skipped_reason (TEXT, widened) |
| 022 | dream_runs.phase_dry_run |
| 023 | merged_into ON DELETE SET NULL |
| 024 | codex_ro readonly view |
| 025 | process_metrics.agent_name |
| 026 | collapse PID PK (agent_name devient PK unique) |
| 027 | idx_consolidation_log_entity_type + idx_indexed_plans_updated_at |
| 028 | tickets + ticket_messages (coordination cross-projet) |
| 029 | ticket_extraction_proposals |
| 030 | curation roadmap : statut archived, features.merged_into, roadmap_curation_proposals |
| 031 | roadmap_curation_proposals.apply_log |
| 032 | project_contexts.focus_revision + trigger CAS + brain_sessions |
| 033 | projects + aliases + registre d'entités + relations canoniques + graph_outbox + triggers/backfill |
| 034 | fencing durable du projecteur Neo4j : singleton v2 + génération/version des claims outbox |
| 035 | interlock de recovery de projection : UUID reprenable, phases PG↔Neo4j et dernier UUID terminé |
| 036 | contrat de lecture Red-Codex : neuf vues `codex_*`, remplacement de `codex_brain_entity_v1`, contraintes et grants `codex_ro` |
| 037 | lifecycle session v4 : garde d'identité, ledger de captures, heartbeat/stale et résultat de focus persisté |
| 038 | journal terminal des tentatives Dream EXTRACT (`ticket_extraction_attempts`) |
| 039 | isolation du trigger de timestamp de `project_contexts` pour préserver les CAS signés |
| 040 | `project_contexts.focus_updated_at`, écrite par le code applicatif, sans backfill |
| 041 | provenance du corpus : `access_log.actor`, `access_count_human` (6 tables), `content_updated_at` (5 tables) écrite par trigger conditionnel `WHEN … IS DISTINCT FROM`, sans backfill |
| 042 | `dream_runs.project_key` (VARCHAR(64), nullable, sans défaut ni backfill) + index `(run_date DESC, project_key)`. `NULL` = écrit avant la 042 ; `'*'` = phase globale, posée par quatre écrivains. Nullable par conséquence : aucun des **six** sites d'INSERT ne fait remonter son échec — trois l'avalent dans leur fonction, deux sont avalés par l'orchestrateur, le sixième est mort et n'est jamais exécuté |
| 043 | `freshness_status_updated_at` (TIMESTAMPTZ, nullable, sans défaut ni backfill) + `freshness_source` (VARCHAR(16), CHECK `NULL OR IN (merge, judgment, score, revive)`) sur les **six** tables suivies par le decay. Écrite par un trigger conditionnel `BEFORE UPDATE OF freshness_status … WHEN (OLD IS DISTINCT FROM NEW)`, gabarit de la 041 et non de la 040 : `freshness_status` a quatre écrivains, dont un prompt passant par le tool générique `brain_update`. Le trigger remet `freshness_source` à `NULL` quand l'écrivain ne la redéclare pas — une provenance absente se voit, une provenance fausse se croit. Préalable DUR de la purge : sans elle `updated_at` redémarre à chaque écriture de compteur et aucune horloge de séjour n'est honnête |
| 044 | `last_accessed_at_human` (TIMESTAMPTZ, nullable, sans défaut ni backfill) sur les six tables suivies par le decay. La 041 avait donné `access_count_human`, qui répare `freq_factor` (poids 0,2) ; elle laissait `access_factor` (poids **0,3**, le plus lourd après l'âge) piloté par les lectures MACHINE — 1 779 learnings mesurés dans ce cas. L'agrégat de `pg_access_log` groupait déjà par acteur : il gagne un `max_accessed_human` dans la boucle qui existe. Consommée derrière `decay_human_signal_enabled`, livré FERMÉ |
| 045 | `dream_runs.model` passe de `varchar(30)` à `varchar(120)`. Deux des cinq modèles de phase configurés n'entraient pas dans 30 car., dont le secours WET **déjà configuré** (`nvidia/nemotron-3-super-120b-a12b`, 33 car.) ; un dépassement lève `StringDataRightTruncation` dans un `INSERT` best-effort, donc c'est la LIGNE entière qui disparaît, pas la colonne. La vue `codex_dream_run_v1` doit tomber et revenir autour de l'`ALTER` — Postgres refuse de retyper une colonne qu'une vue projette — et son `GRANT SELECT` à `codex_ro` est reposé, un `DROP VIEW` emportant ses droits. Aucune table ajoutée, aucune donnée touchée. Downgrade fail-closed si des lignes dépassent 30 car. |

## Requêtes types

### Full-text search avec ranking

```sql
SELECT *, ts_rank(search_vector, plainto_tsquery('english', $1)) AS rank
FROM decisions
WHERE search_vector @@ plainto_tsquery('english', $1)
  AND ($2::varchar IS NULL OR project_key = $2)
  AND ($3::varchar IS NULL OR status = $3)
ORDER BY rank DESC
LIMIT $4 OFFSET $5;
```

### Recherche vectorielle (pgvector)

```sql
-- Semantic search : top-K par cosine similarity
-- op('<=>',return_type=sa.Float) : ADR #8 — change uniquement le result-processor Python,
-- n'émet AUCUN CAST SQL (le plan HNSW est préservé).
SELECT *, 1 - (embedding <=> $1::vector) AS similarity
FROM decisions
WHERE embedding IS NOT NULL
  AND ($2::varchar IS NULL OR project_key = $2)
ORDER BY embedding <=> $1::vector
LIMIT $3;
```

**Note**: `<=>` est la cosine distance (1 - cosine_similarity). `op('<=>',return_type=sa.Float)` dans SQLAlchemy (ADR #8, `pg_base.py:528`) change uniquement le result processor Python — sans lui, pgvector renvoie `bytea` et le tri échoue. Aucun `CAST` n'est émis dans le SQL généré (ce qui préserve le plan HNSW).

### Chaîne de supersession (recursive CTE)

```sql
-- Toute la chaîne depuis une décision
WITH RECURSIVE chain AS (
    SELECT id, title, status, superseded_by, 1 AS depth
    FROM decisions WHERE id = $1
    UNION ALL
    SELECT d.id, d.title, d.status, d.superseded_by, c.depth + 1
    FROM decisions d
    JOIN chain c ON d.id = c.superseded_by
)
SELECT * FROM chain ORDER BY depth;
```

### Comptage pour refresh_counts

```sql
UPDATE project_contexts SET
    decisions_count = (SELECT COUNT(*) FROM decisions WHERE project_key = $1),
    learnings_count = (SELECT COUNT(*) FROM learnings WHERE project_key = $1),
    snippets_count = (SELECT COUNT(*) FROM snippets WHERE project_key = $1),
    runbooks_count = (SELECT COUNT(*) FROM runbooks WHERE project_key = $1),
    adrs_count = (SELECT COUNT(*) FROM adrs WHERE project_key = $1)
WHERE project_key = $1
RETURNING *;
```

## Embedding — Text generation par entity type

| Entity | Champs concaténés pour embedding |
|--------|----------------------------------|
| Decision | `{title} {description} {reasoning} {' '.join(alternatives)} {' '.join(tags)}` |
| Learning | `{topic} {insight} {source or ''} {' '.join(tags)}` |
| Snippet | `{title} {intention} {language} {' '.join(tags)}` |
| Runbook | `{title} {description} {trigger} {' '.join(tags)}` |
| ADR | `{title} {context} {decision} {consequences} {' '.join(tags)}` |
| PlanChunk | `{section_title} {content}` (tronqué à 15 000 chars max dans `plan_indexer.py`) |
| ProjectContext | Pas d'embedding |

## GPU Embedding Service

```python
class GPUEmbeddingService:
    """Embedding service using HTTP GPU inference (Qodo-Embed-1-1.5B)."""

    def __init__(
        self,
        base_url: str = "http://localhost:8003",  # défaut constructeur
        timeout: float = 30.0,
        max_retries: int = 3,
    ) -> None: ...

    async def embed(self, text: str) -> list[float]: ...
    async def embed_batch(self, texts: list[str]) -> list[list[float]]: ...
```

**Note**: Le défaut `Settings.embedding_service_url` et celui du constructeur sont tous deux `http://localhost:8003`. Le chemin `deploy/dev-pc` est une référence de rollback obsolète depuis le retour au service GPU local du 6 juillet 2026. L'interface reste `embed()` / `embed_batch()` en async via httpx. La constante `DIMENSION = 1536` n'est pas définie dans `GPUEmbeddingService` — la dimension est configurée via `Settings.embedding_dimension`.
