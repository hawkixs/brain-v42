# Design: Tests de performance & Migration des données

**Date**: 2026-03-02
**Statut**: Approuvé
**Scope**: brain_v42 — post-M5 (production-ready)

## Contexte

brain_v42 est production-ready (M1-M5 terminés, 800 tests, 95% coverage). Deux tâches restantes :
1. Valider les performances du nouveau système (baseline, comparatif avec l'ancien, load testing, qualité des recherches)
2. Migrer les données de l'ancien brain (Neo4j/datalake_v2) vers le nouveau (PostgreSQL/brain_v42)

## Tâche 1 : Script de benchmark

### Fichier

`scripts/benchmark.py` — script standalone avec 4 modes.

### Mode: baseline

Mesure les latences de chaque opération brain-v42 sur PostgreSQL :
- **CRUD** : create, get_by_id, update, list_all, delete × 6 entity types (decisions, learnings, snippets, runbooks, adrs, project_contexts)
- **FTS** : recherche full-text via tsvector
- **Semantic search** : recherche vectorielle via pgvector HNSW
- **Global search** : brain_service.search() et what_do_i_know_about()
- **Embedding** : temps de génération ONNX (all-MiniLM-L6-v2)

Métriques par opération : min, avg, p50, p95, p99, max latency.

### Mode: compare

Benchmark comparatif ancien (Neo4j) vs nouveau (PostgreSQL) :
- Mêmes opérations exécutées sur les 2 backends
- Ancien : appel direct Neo4j via neo4j-driver
- Nouveau : appel direct repos PG via SQLAlchemy async
- Output : tableau side-by-side avec ratio de performance

### Mode: load

Test de charge avec workers concurrents :
- asyncio.gather() avec N workers
- Mix d'opérations : 70% read, 20% search, 10% write
- Paramètres : --concurrency (défaut 10), --duration (défaut 30s)
- Métriques : throughput (ops/sec), p50, p95, p99, error rate

### Mode: quality

Validation de la qualité des recherches :
- **Precision test** : requêtes connues avec résultats attendus, mesure precision@5
- **Noise test** : requêtes vagues, vérifie que le nombre de résultats reste raisonnable et que les scores de similarité décroissent proprement
- **FTS vs Semantic** : même requête via les 2 moteurs, compare overlap et ranking
- **Cutoff recommendation** : suggère un seuil de cosine distance optimal
- Métriques : precision@5, MRR (Mean Reciprocal Rank), noise ratio

### CLI

```bash
# Baseline brain-v42
python scripts/benchmark.py --mode baseline

# Comparatif ancien vs nouveau
python scripts/benchmark.py --mode compare --neo4j-uri bolt://localhost:7687

# Load test
python scripts/benchmark.py --mode load --concurrency 10 --duration 30

# Quality check
python scripts/benchmark.py --mode quality

# Tous les modes
python scripts/benchmark.py --mode all --neo4j-uri bolt://localhost:7687
```

### Output

- Console : tableaux formatés (tabulate)
- Fichier : `results/<timestamp>_benchmark.json`

### Dépendances

- `tabulate` (formatage console)
- `neo4j` (driver pour mode compare)

## Tâche 2 : Migration des données

### Script existant

`scripts/migrate_neo4j_to_pg.py` (feature 640, déjà mergé).

### Volume de données (ancien brain)

| Type | Count |
|------|-------|
| Decisions | ~75 |
| Learnings | ~100 |
| Snippets | ~20 |
| Runbooks | 4 |
| ADRs | 1 |
| Project Contexts | ~15 |
| **Total** | **~215** |

### Plan d'exécution

#### 1. Pré-migration
- Vérifier brain_v42 PG up (port 5433)
- Vérifier Neo4j up (port 7687)
- Backup PG : `pg_dump`
- Dry-run : `python scripts/migrate_neo4j_to_pg.py --dry-run`

#### 2. Migration
- Lancer avec `--regen-embeddings` (sentence-transformers 768d → ONNX 384d)
- Batch size : 50 (défaut)
- ON CONFLICT DO NOTHING (idempotent)
- UUIDs préservés

#### 3. Validation post-migration
- Count par type : ancien vs nouveau (doivent matcher)
- Spot-check : 3-5 entrées par type
- Vérifier embeddings non-null
- Tester recherche semantic sur données migrées

#### 4. Bascule
- Désactiver mcp-brain dans la config MCP globale
- Garder uniquement brain-v42
- Mettre à jour le project_context
- Optionnel : arrêter containers datalake_v2

### Points d'attention

- **Embeddings incompatibles** : L'ancien utilise sentence-transformers (768 dims), le nouveau ONNX all-MiniLM-L6-v2 (384 dims). `--regen-embeddings` est obligatoire.
- **Idempotent** : ON CONFLICT DO NOTHING permet de relancer sans risque.
- **UUIDs préservés** : Les références croisées restent valides.

## Ordre d'exécution

1. Écrire le script de benchmark
2. Exécuter la migration (dry-run puis réelle)
3. Lancer les benchmarks (baseline sur données réelles)
4. Lancer quality check
5. Lancer compare (ancien vs nouveau)
6. Bascule complète vers brain-v42
