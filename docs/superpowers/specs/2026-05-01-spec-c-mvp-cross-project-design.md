# Spec C MVP β — Cross-Project Briefing & Resonance Detection

**Date** : 2026-05-01 (v2 post-multi-judge critique)
**Status** : Implemented 2026-06-12 (plan: docs/plans/2026-06-12-spec-c-cross-project-resonance.md) — killswitch closed, rollout pending
**Branch** : `feat/dream-cross-project-resonance-and-briefing`
**Killswitch** : `BRAIN_DREAM_CROSS_PROJECT_ENABLED=false` (closed by default)

## Context

Le brain MCP (brain-v42) accumule de la connaissance per-project. Aujourd'hui, le Layer-2 domain registry (9 closed domains : infra, ml, backend, memory, tooling, data, ops, frontend, security) est opérationnel et déjà global au niveau Neo4j (`Domain {name}` n'est pas keyé sur `project_key`). Les repos PG acceptent déjà `project_keys: list[str]` pour multi-project filtering. Mais l'utilisateur (humain ou Claude) n'a aucune surface qui exploite cette topologie cross-project.

Spec C précédente (full) couvrait 4 angles. Ce MVP β scope deux angles seulement, choisis pour leurs bénéfices primaires :
- **Knowledge transfer** : surfacer les insights pertinents d'autres projets quand on travaille sur le projet courant
- **Resonance detection** (drift OR convergence candidates) : détecter les paires de décisions cross-project intra-domaine à haut cosinus, l'algorithme ne tranche pas le verdict (humain interprète)

## Goals & Non-Goals

### Goals (MVP)
1. Enrichir `brain_session_start` avec une section "Cross-project insights" listant top-N entités d'autres projets dans les domaines actifs du projet courant.
2. Ajouter un script `scripts/dream/cross_project_resonance.py` qui produit un rapport markdown nightly DRY_RUN-able des paires de décisions cross-project à cosine >= seuil.
3. Killswitch fermé par défaut. Aucune écriture sans flag explicite + env var.
4. Aucun nouveau tool MCP. Aucun changement de schéma DB ni Neo4j.
5. Suite de tests existante (1837) reste verte. Nouveau code couvert TDD.

### Non-Goals
- Pas de modif `brain_search` runtime (option α/δ — réservé pour itération future si MVP fonctionne).
- Pas de tool MCP `brain_search_cross_project` ni équivalent (constraint utilisateur).
- Pas d'écriture brain_learn automatique : DRY_RUN par défaut, mode WET nécessite review humaine sur ≥ 5 nuits.
- Pas de cross-project warning inline dans `brain_log_decision` (γ/δ futurs).
- Pas d'intégration cron (manuel d'abord, cron en follow-up séparé).
- Pas d'integration tests pour MVP (unit suffit).

## Preconditions (verified during exploration)

Le spec assume les invariants suivants. Si l'un casse, il faut un guard explicite (TBD pendant le plan) :

| Invariant | Vérifié | Si KO |
|-----------|---------|-------|
| `decisions.embedding` est `Vector(1536)` mais peut être NULL (cf. `pg_decision.py:245`) | ✅ | Le script DOIT filtrer `WHERE embedding IS NOT NULL` |
| Tous les Decisions cross-project ont été embeddés avec **Qodo-Embed-1.5B** (1 seul modèle dans le codebase, cf. CLAUDE.md) | ✅ | Si modèle change un jour : recalcul intégral nécessaire — out of MVP |
| Domain nodes globaux Neo4j keyés par `name` seul (cf. `graph_service.py:299`) | ✅ | — |
| Edges `BELONGS_TO_DOMAIN` créés via Layer-2 (CONNECT step B agent-driven) | ✅ | Entités non encore classifiées sont silencieusement exclues du périmètre — comportement acceptable |
| Constante `ALLOWED_DOMAINS` exportable depuis `services/graph_service.py` | ✅ | Réutilisée tel quelle dans le script |
| `Project {project_key}` Neo4j node existe pour chaque projet actif | À vérifier au plan | Cypher renvoie 0 résultats si manquant — section briefing omise (graceful) |
| Module thresholds : `src/brain_v42/thresholds.py` avec `by_name(name) -> ThresholdSpec | None` (cf. ligne 146) | ✅ | Spec utilise `by_name("cross_project_resonance_min").value` |
| pgvector `<=>` operator natif sur `decisions.embedding` (cf. `pg_decision.py:247`) | ✅ | Pair-cosine fait en PG, pas en Python (cf. décision archi ci-dessous) |

## Architecture

### Composants modifiés
- `src/brain_v42/mcp/tools/session_tools.py` — `_format_session_briefing` gagne param optionnel `cross_entries`. Nouvelle fonction privée `_fetch_cross_project_entries(...)` qui interroge Neo4j + PG.
- `src/brain_v42/services/graph_service.py` — Deux nouvelles méthodes : `fetch_active_domains(project_key, top_n)` et `fetch_cross_project_entity_ids(domains, exclude_project_key, limit)` (retourne uniquement IDs + types + project_key, pas les corps).
- `src/brain_v42/repositories/pg_decision.py` — Nouvelle méthode `fetch_with_embeddings_by_ids(ids: list[UUID]) -> list[DecisionWithEmbedding]` (pair compute) + `fetch_brief_by_ids(ids: list[UUID]) -> list[DecisionBrief]` (briefing display).
- `src/brain_v42/repositories/pg_learning.py`, `pg_snippet.py`, `pg_runbook.py`, `pg_adr.py` — Symétriques `fetch_brief_by_ids(...)` pour briefing display (1 round-trip par type).
- `src/brain_v42/thresholds.py` — Une nouvelle entrée `cross_project_resonance_min` dans `REGISTRY` (value=0.80, calibrated=False).
- `src/brain_v42/config.py` — Trois nouvelles env vars + helper de lecture.

### Composants créés
- `scripts/dream/cross_project_resonance.py` — Script CLI `python -m scripts.dream.cross_project_resonance [--mode dry_run|wet] [--domains ml,memory] [--date YYYY-MM-DD]`.
- Tests unitaires correspondants dans `tests/unit/`.

### Composants intacts
- Schéma PG (decisions, learnings, snippets, runbooks, adrs) — aucune migration.
- Schéma Neo4j (Domain, Project, BELONGS_TO, BELONGS_TO_DOMAIN) — aucun changement.
- 30 tools MCP existants — aucun ajout, aucune modif d'API publique.
- `brain_search` — intact dans MVP.
- `dream_runs` table — pas modifiée mais le script utilise `INSERT INTO dream_runs (...) RETURNING id` pour traçabilité (pattern existant).

### Data flow

**Briefing (`brain_session_start`)** :
```
client → brain_session_start(project_key)
  → project_context_svc.get_by_key(project_key)
  → decision_svc.list_all(project_key, limit=5)
  → learning_svc.list_all(project_key, limit=5)
  → IF env CROSS_PROJECT_ENABLED:
      → graph_service.fetch_active_domains(project_key, top_n=cfg.TOP_N)  # default 2
      → graph_service.fetch_cross_project_entity_ids(domains, exclude=project_key, limit=cfg.MAX)  # default 5
      → grouped_ids = {Decision: [...], Learning: [...], ...}
      → cross_entries = []
      → FOR (entity_type, ids) in grouped_ids:  # 1 PG round-trip per type
          → cross_entries.extend(repo_for(entity_type).fetch_brief_by_ids(ids))
      → cross_entries.sort(key=created_at desc)
    ELSE: cross_entries = None
  → _format_session_briefing(ctx, decisions, learnings, cross_entries)
  → return markdown
```

**Resonance script (`cross_project_resonance.py`)** :
```
CLI invocation
  → check env CROSS_PROJECT_ENABLED (else exit 0 fast, log "disabled")
  → INSERT INTO dream_runs (kind='cross_project_resonance', mode=$mode, started_at=now()) RETURNING run_id
  → threshold = thresholds.by_name("cross_project_resonance_min").value  # 0.80
  → target_domains = --domains or ALLOWED_DOMAINS  # 9
  → all_pairs: list[ResonancePair] = []
  → FOR domain IN target_domains:
      → entity_ids = graph_service.fetch_decision_ids_in_domain_across_projects(domain)  # Neo4j filter
      → IF len(entity_ids) < MIN_DECISIONS_PER_DOMAIN: skip  # 5
      → pairs = pg_decision.fetch_cross_project_resonance_pairs(  # PG-side cosine via <=>
            ids=entity_ids,
            threshold=threshold,
            limit=MAX_DECISIONS_PER_DOMAIN  # 200, hard cap to bound cost
        )
      → all_pairs.extend(pairs annotated with domain)
  → all_pairs.sort(key=cosine desc)[:MAX_PAIRS_PER_NIGHT]  # 20
  → write_markdown_report(path = artifacts/dream/cross_project_resonance_<UTC-date>.md)  # overwrite if exists
  → IF --mode wet:
      → IF NOT env_enabled(): log_error + exit 1 (defensive double-check)
      → FOR pair IN all_pairs:
          → dedup_key = sha256(f"{min(a_id, b_id)}|{max(a_id, b_id)}|{domain}")
          → IF learning_repo.exists_by_dedup_key(dedup_key): skip (idempotency)
          → learning_repo.create(
                topic=f"cross_project_resonance/{pair.domain}",
                insight=pair.format_insight(),
                tags=["dream", "cross_project_resonance", pair.domain, "EXCLUDE_FROM_PROMOTE"],
                project_key="brain-v42",
                source_kind="cross_project_resonance",  # for PROMOTE/CONSOLIDATION exclusion filter
                dedup_key=dedup_key,
                dream_run_id=run_id,
            )
  → UPDATE dream_runs SET ended_at=now(), pair_count=$N WHERE id=$run_id
  → exit 0
```

### Feedback-loop insulation (WET mode)

Risque identifié (J2) : `brain_learn(project_key="brain-v42", ...)` ré-entre dans le pipeline PROMOTE puis CONSOLIDATION qui pourrait fusionner les learnings de résonance entre eux et corrompre la généalogie.

Mitigation MVP :
- **Tag `EXCLUDE_FROM_PROMOTE`** + **`source_kind="cross_project_resonance"`** sur chaque learning émis
- PROMOTE phase (existing `promote_prepare.py:fetch_candidates`) **DOIT** filtrer `WHERE source_kind != 'cross_project_resonance' AND 'EXCLUDE_FROM_PROMOTE' NOT IN tags` — **modification PROMOTE est INCLUE dans cette MR** (sinon WET reste bloqué pour toujours)
- Tests : `test_promote_excludes_cross_project_resonance_learnings`

Si la colonne `source_kind` n'existe pas sur `learnings` : utiliser uniquement le tag `EXCLUDE_FROM_PROMOTE` comme single-source-of-truth (et adapter `fetch_candidates` en conséquence). Décision finale au plan d'implémentation après vérification du schéma.

## Detailed Design

### A. `brain_session_start` cross-project briefing

**Format additif** (après le bloc actuel, séparé par blank line) :

```
**Cross-project (ml, memory):**
- [red-shrik] Decision · 2026-04-28 · embedding healthcheck pattern
- [red-shrik] Learning · 2026-04-22 · cosine 0.85 retient les vrais clusters
- [red-monitor] Learning · 2026-04-15 · go-pubsub close channel race
```

**Display field mapping** (per entity type) :

| Entity type | Display field | Truncation | Notes |
|-------------|---------------|------------|-------|
| Decision | `title` | 60 chars + `…` | — |
| Learning | `topic` | 60 chars + `…` | — |
| Snippet | `intent` | 60 chars + `…` | — |
| Runbook | `name` | 60 chars + `…` | — |
| ADR | `title` | 60 chars + `…` | — |

**Sélection** :
1. `domains_actifs` = top-N domaines du projet courant, comptés via Neo4j :
   ```cypher
   MATCH (e)-[:BELONGS_TO]->(:Project {project_key: $current})
   MATCH (e)-[:BELONGS_TO_DOMAIN]->(d:Domain)
   WITH d.name AS domain, count(e) AS n
   ORDER BY n DESC LIMIT $top_n
   RETURN domain
   ```
2. `cross_ids` = entités d'autres projets dans ces domaines, par recency desc :
   ```cypher
   MATCH (e)-[:BELONGS_TO_DOMAIN]->(d:Domain)
   WHERE d.name IN $domains
   MATCH (e)-[:BELONGS_TO]->(p:Project)
   WHERE p.project_key <> $current
   RETURN e.id AS id, labels(e) AS types, p.project_key AS project, e.created_at AS created_at
   ORDER BY e.created_at DESC LIMIT $entries_max
   ```
3. Group `cross_ids` by entity type → 1 PG round-trip per type via `fetch_brief_by_ids(ids)` (max 5 types × 1 query = 5 queries worst case ; typically 1-2 since `entries_max=5`).
4. Re-merge + sort by `created_at` desc, format markdown.

**Empty states** :
- Pas de domaine actif → section omise.
- Pas d'autres projets dans ces domaines → section omise.
- Neo4j down → log warning + section omise (briefing ancien retourné intact).
- Env var OFF → section omise (zero overhead).

**Backward compat** : `_format_session_briefing(ctx, decisions, learnings, cross_entries=None)`. Param ajouté en fin avec default `None` — appel actuel inchangé, output identique si `cross_entries` falsy.

**Latency note** : add 2 Cypher queries + 1-2 PG queries to the briefing path. Soft target : briefing p99 < 500ms. No timeout/SLO enforcement in MVP — measured during rollout J+1, optimize via caching if needed.

### B. `cross_project_resonance.py` script

**Vocabulaire** : "resonance" — l'algorithme surface les paires haut-cosinus, ne tranche pas convergence vs drift. Heuristique numérique (regex `\d+\.\d+`) propose un hint optionnel. Le terme "drift" n'apparaît plus dans noms de fichiers, branche, env vars, ni headings.

**`ResonancePair` dataclass** :

```python
from dataclasses import dataclass
from uuid import UUID

@dataclass(frozen=True)
class ResonancePair:
    a_id: UUID
    b_id: UUID
    a_project: str
    b_project: str
    a_title: str
    b_title: str
    a_created_at: date
    b_created_at: date
    cosine: float
    domain: str  # e.g. "ml"

    @property
    def hint(self) -> str:
        """Heuristic only, never authoritative."""
        nums_a = set(re.findall(r"\d+\.\d+", self.a_title))
        nums_b = set(re.findall(r"\d+\.\d+", self.b_title))
        if nums_a and nums_b and nums_a != nums_b:
            return f"drift candidate (numeric divergence: {nums_a} vs {nums_b})"
        return "convergence likely (no numeric divergence detected)"

    @property
    def dedup_key(self) -> str:
        """SHA256 of canonical pair fingerprint for WET idempotency."""
        lo, hi = sorted([str(self.a_id), str(self.b_id)])
        return hashlib.sha256(f"{lo}|{hi}|{self.domain}".encode()).hexdigest()

    def format_insight(self) -> str:
        """Body for brain_learn.insight in WET mode."""
        return (
            f"Cross-project resonance in domain '{self.domain}' (cosine={self.cosine:.3f}):\n"
            f"- [{self.a_project}] {self.a_title} ({self.a_created_at})\n"
            f"- [{self.b_project}] {self.b_title} ({self.b_created_at})\n"
            f"Hint: {self.hint}"
        )
```

**Algorithme** :
```python
def main(mode: str, domains: list[str] | None, date_str: str | None) -> int:
    if not env_enabled():
        log("cross-project disabled, exiting")
        return 0

    threshold_spec = thresholds.by_name("cross_project_resonance_min")
    if threshold_spec is None:
        log_error("threshold registry missing 'cross_project_resonance_min'")
        return 1
    threshold = threshold_spec.value  # 0.80

    target_domains = domains or sorted(graph_service.ALLOWED_DOMAINS)  # 9

    run_id = await dream_runs_repo.start_run(kind="cross_project_resonance", mode=mode)

    try:
        all_pairs: list[ResonancePair] = []
        for domain in target_domains:
            ids = await graph_service.fetch_decision_ids_in_domain_across_projects(domain)
            if len(ids) < MIN_DECISIONS_PER_DOMAIN:  # 5
                continue
            pairs = await pg_decision.fetch_cross_project_resonance_pairs(
                ids=ids[:MAX_DECISIONS_PER_DOMAIN],  # 200
                threshold=threshold,
                domain=domain,
            )
            all_pairs.extend(pairs)

        all_pairs.sort(key=lambda p: p.cosine, reverse=True)
        all_pairs = all_pairs[:MAX_PAIRS_PER_NIGHT]  # 20

        report_path = build_report_path(date_str)  # artifacts/dream/cross_project_resonance_<UTC-iso-date>.md
        write_markdown_report(report_path, all_pairs, threshold, overwrite=True)

        if mode == "wet":
            if not env_enabled():  # defensive double-check
                log_error("WET blocked: env disabled")
                await dream_runs_repo.end_run(run_id, status="blocked", pair_count=0)
                return 1
            written = 0
            for pair in all_pairs:
                if await learning_repo.exists_by_dedup_key(pair.dedup_key):
                    continue  # idempotent
                await learning_repo.create(
                    topic=f"cross_project_resonance/{pair.domain}",
                    insight=pair.format_insight(),
                    tags=["dream", "cross_project_resonance", pair.domain, "EXCLUDE_FROM_PROMOTE"],
                    project_key="brain-v42",
                    source_kind="cross_project_resonance",
                    dedup_key=pair.dedup_key,
                    dream_run_id=run_id,
                )
                written += 1
            await dream_runs_repo.end_run(run_id, status="completed", pair_count=written)
        else:
            await dream_runs_repo.end_run(run_id, status="completed", pair_count=len(all_pairs))
    except Exception as e:
        await dream_runs_repo.end_run(run_id, status="error", pair_count=0)
        raise

    return 0
```

**PG-side pair computation** (replaces Python O(n²), per J2 critique with the simpler-but-bounded variant) :

```python
# In pg_decision.py
async def fetch_cross_project_resonance_pairs(
    self, *, ids: list[UUID], threshold: float, domain: str
) -> list[ResonancePair]:
    """Compute all cross-project pairs above threshold via pgvector <=>.

    Bounded by ids list (capped at MAX_DECISIONS_PER_DOMAIN upstream).
    Excludes intra-project pairs in SQL.
    """
    query = text("""
        SELECT
            a.id AS a_id, b.id AS b_id,
            a.project_key AS a_project, b.project_key AS b_project,
            a.title AS a_title, b.title AS b_title,
            a.created_at::date AS a_created_at, b.created_at::date AS b_created_at,
            (1 - (a.embedding <=> b.embedding))::float AS cosine
        FROM decisions a
        JOIN decisions b ON a.id < b.id  -- avoid self + duplicate pairs
        WHERE a.id = ANY(:ids) AND b.id = ANY(:ids)
          AND a.project_key <> b.project_key
          AND a.embedding IS NOT NULL AND b.embedding IS NOT NULL
          AND (1 - (a.embedding <=> b.embedding)) >= :threshold
        ORDER BY cosine DESC
    """)
    rows = await self._execute(query, {"ids": ids, "threshold": threshold})
    return [ResonancePair(domain=domain, **dict(r)) for r in rows]
```

Cost : worst case 200×200/2 = 20k pairs evaluated per domain inside PG, well within pgvector's capability. No embedding payload moves to Python.

**Markdown output** :
```markdown
# Cross-Project Resonance — 2026-05-01

Threshold: 0.80 · Pairs found: 7 · Domains scanned: 9 · Domains with pairs: 4 · Run ID: <uuid>

## Domain: ml (3 pairs)

### Pair 1 — cosine=0.91
- [brain-v42] Decision a3f2e... · "Use Qodo-Embed-1.5B" · 2026-04-15
- [red-shrik] Decision 8c1d4... · "Qodo-Embed for code embedding" · 2026-04-22
- Hint: convergence likely (no numeric divergence detected)

### Pair 2 — cosine=0.83
- [brain-v42] Decision e51fa... · "Cosine 0.92 for dedup" · 2026-04-10
- [red-shrik] Decision 7b9c0... · "Cosine 0.85 for dedup" · 2026-03-28
- Hint: drift candidate (numeric divergence: 0.92 vs 0.85)
```

Empty case (zero pairs) :
```markdown
# Cross-Project Resonance — 2026-05-01

Threshold: 0.80 · Pairs found: 0 · Domains scanned: 9 · Domains with pairs: 0 · Run ID: <uuid>

No cross-project resonance pairs above threshold this run.
```

**File policy** : path = `artifacts/dream/cross_project_resonance_<UTC-ISO-date>.md` (e.g. `2026-05-01`). Overwrite if exists (idempotent re-run produces identical content).

### C. Threshold registry

Nouvelle entrée dans `src/brain_v42/thresholds.py:REGISTRY` :
```python
ThresholdSpec(
    name="cross_project_resonance_min",
    value=0.80,
    domain="dream",
    rationale="Min cosine to surface decision pair as cross-project resonance candidate",
    calibrated=False,
)
```

Initial 0.80 deliberately below 0.85 (dedup threshold). Recalibrer après 5+ nuits de DRY_RUN.

Lookup pattern : `thresholds.by_name("cross_project_resonance_min").value` (returns `ThresholdSpec | None`, defensive `if spec is None: return 1` in script).

## Configuration

| Var | Default | Effet |
|-----|---------|-------|
| `BRAIN_DREAM_CROSS_PROJECT_ENABLED` | `false` | Master switch. OFF → briefing skip, script exit fast |
| `BRAIN_CROSS_PROJECT_BRIEFING_DOMAINS_TOP_N` | `2` | Top-N domaines actifs surfacés dans briefing |
| `BRAIN_CROSS_PROJECT_BRIEFING_ENTRIES_MAX` | `5` | Cap entrées briefing |

Constants script (non env, code-local) :
- `MIN_DECISIONS_PER_DOMAIN = 5`
- `MAX_DECISIONS_PER_DOMAIN = 200` (per J2 graceful-degradation cap)
- `MAX_PAIRS_PER_NIGHT = 20`

CLI defaults (`argparse`) :
- `--mode` default = `"dry_run"` (explicit)
- `--domains` default = `None` (=all 9)
- `--date` default = `None` (=today UTC)

Threshold cosine : exclusivement via `thresholds.by_name("cross_project_resonance_min").value`, pas d'env var.

## Safety & Rollback

1. **Killswitch fermé** : MR mergeable sans risque prod. Activation = `export BRAIN_DREAM_CROSS_PROJECT_ENABLED=true`.
2. **DRY_RUN par défaut** : `--mode dry_run` est argparse default ; `--mode wet` nécessite intent explicite.
3. **Triple safeguard WET** : (a) env var, (b) `--mode wet` flag, (c) inner re-check `env_enabled()` inside WET branch → `exit 1`.
4. **Backward compat briefing** : `cross_entries=None` → output identique au comportement actuel.
5. **Graceful degradation** : Neo4j down dans briefing → section omise, briefing ancien intact ; PG/embedding fail dans script → `dream_runs.status='error'` + raise (no partial writes).
6. **WET idempotency** : `dedup_key = sha256(sorted(ids) + domain)` empêche les doublons re-run même nuit.
7. **PROMOTE/CONSOLIDATION insulation** : tag `EXCLUDE_FROM_PROMOTE` + `source_kind="cross_project_resonance"` filtrés explicitement par `promote_prepare.fetch_candidates` (modif INCLUSE dans MR).
8. **Rollback** : revert MR. Aucune migration à reverse.

## Rollout

```
J+0    : MR mergée, killswitch fermé (no-op en prod)
J+1    : export ENABLED=true localement, vérifier briefing sur 2-3 sessions, mesurer p99 latency
J+2-5  : run script DRY_RUN chaque soir manuellement, review .md
J+6+   : si signal utile et 0 false-positive aberrant → considérer WET (vérifier filter PROMOTE actif)
J+10+  : si WET stable → ajouter cron nightly (follow-up MR séparée)
```

## Test Surface

### Unit tests — briefing

- `test_briefing_skips_cross_section_when_flag_off`
- `test_briefing_skips_cross_section_when_no_active_domains`
- `test_briefing_skips_cross_section_when_no_other_projects_in_domains`
- `test_briefing_includes_top_n_domains_only` (4 domaines disponibles, assertion exactement 2 ressortent)
- `test_briefing_top_n_respects_env_var` (override via env)
- `test_briefing_excludes_current_project`
- `test_briefing_orders_entries_by_recency_desc`
- `test_briefing_caps_entries_at_max`
- `test_briefing_format_includes_project_label_type_date_title`
- `test_briefing_truncates_display_field_at_60_chars`
- `test_briefing_graceful_when_neo4j_fails` (mock raises `Neo4jError` → section omise, briefing ancien complet)
- `test_briefing_param_optional_backward_compat`

### Unit tests — script

- `test_pair_decisions_excludes_intra_project` (PG SQL contains `a.project_key <> b.project_key`)
- `test_pair_decisions_respects_threshold`
- `test_pair_decisions_caps_at_max_pairs_per_night`
- `test_pair_decisions_skips_domains_below_min_decisions`
- `test_pair_decisions_caps_per_domain_at_max_decisions` (200)
- `test_pair_decisions_skips_null_embeddings` (PG `WHERE embedding IS NOT NULL`)
- `test_dry_run_writes_markdown_report_no_brain_learn`
- `test_wet_mode_writes_brain_learn_per_pair_with_dedup_key`
- `test_wet_mode_idempotent_on_rerun_same_date` (2nd run writes 0 new learnings)
- `test_wet_mode_blocked_when_env_disabled` (inner guard, returns 1)
- `test_dry_run_blocked_when_env_disabled` (outer guard, returns 0)
- `test_report_format_groups_by_domain_with_counts`
- `test_report_format_with_zero_pairs` (empty-case markdown)
- `test_report_includes_threshold_and_metadata_and_run_id`
- `test_report_overwrites_existing_file_same_date`
- `test_resonance_pair_dedup_key_stable_across_id_order` (sorting invariant)
- `test_resonance_pair_format_insight_includes_hint`
- `test_resonance_pair_hint_drift_when_numeric_divergence`
- `test_resonance_pair_hint_convergence_when_no_divergence`
- `test_dream_runs_row_inserted_then_completed`
- `test_dream_runs_row_marked_error_on_exception`

### Unit tests — threshold registry

- `test_cross_project_resonance_threshold_present` (entry exists in REGISTRY, value=0.80, calibrated=False)
- `test_threshold_lookup_via_by_name_returns_value`

### Unit tests — PROMOTE insulation

- `test_promote_excludes_cross_project_resonance_learnings` (fetch_candidates filters tag/source_kind)

### Integration tests

Out of MVP. Réintroduire si/quand WET mode ouvre.

### Coverage

Cible : maintenir le `60%` minimum CI. Estimation révisée v2 : ~350-450 lignes nouveau code (PG SQL + ResonancePair + script + repo methods + PROMOTE filter), ~550-650 lignes tests (~30 unit tests).

### Non-regression

`pytest tests/unit -v` doit retourner 1837/1837 baseline + nouveaux tests verts à chaque commit.

## Open Questions Resolved

| Q | Decision |
|---|----------|
| Cross-project topologie nouvelle ou existante ? | Existante (domains globaux Neo4j, repos `project_keys` PG) |
| Nouveaux tools MCP ? | Non (constraint utilisateur) |
| Schema migration ? | Non (rien à migrer ; ajout colonne `source_kind` sur learnings : à vérifier au plan, possible no-op si déjà présent) |
| Surface briefing : passive (search) ou ponctuelle (session_start) ? | Ponctuelle (session_start) |
| Surface drift : sync (warning) ou async (script nightly) ? | Async (script DRY_RUN-able) |
| Threshold valeur initiale ? | 0.80, calibrated=False, ré-évaluation après 5+ nuits |
| WET autorisé d'office ? | Non (DRY_RUN d'abord, WET opt-in après review) |
| Cron nightly d'office ? | Non (manuel d'abord, cron en follow-up MR) |
| Pair compute Python ou PG ? | **PG** (pgvector `<=>` natif, capped par `MAX_DECISIONS_PER_DOMAIN=200`) |
| Terminologie partout ? | "resonance" (branch, script, env vars, headings) — "drift" uniquement comme hint heuristique |
| Threshold registry path/API ? | `src/brain_v42/thresholds.py` + `by_name(name).value` (frozen dataclass `ThresholdSpec`) |
| WET idempotency ? | `dedup_key = sha256(sorted(ids) + domain)` |
| Feedback loop PROMOTE ? | Filter `tag=EXCLUDE_FROM_PROMOTE` + `source_kind=cross_project_resonance` dans `fetch_candidates` (modif INCLUSE) |

## Follow-ups (out of MVP, captured for tracking)

1. Si MVP β fonctionne : étendre brain_search avec param `cross_project=False` (option α de la matrice originale).
2. Si MVP β fonctionne : warning inline dans brain_log_decision (option γ/δ).
3. Cron nightly du script résonance (séparé de cette MR).
4. Recalibrage du threshold `cross_project_resonance_min` après 5+ nuits de data.
5. Étendre la résonance aux Learnings et ADRs (pas seulement Decisions).
6. Bridge insights generator dans SYNTH (option B initial — non retenu pour MVP car nécessite WET stable).
7. Cache `fetch_active_domains` per (project_key, hour) si briefing latency p99 > 500ms.
8. Embedding model fingerprint column si plusieurs modèles cohabitent un jour.

## Changelog

- **v1 (2026-05-01)** — Initial design after brainstorm.
- **v2 (2026-05-01)** — Multi-judge critique applied : terminology consistency (drift→resonance), threshold path corrected (`thresholds.py:by_name`), `ResonancePair` dataclass defined with `format_insight`/`dedup_key`/`hint`, PG-side pair compute via pgvector `<=>` (replaces Python O(n²)), preconditions section added, WET idempotency via dedup_key, feedback-loop insulation via `EXCLUDE_FROM_PROMOTE` tag + `source_kind`, `dream_runs` traceability, CLI defaults explicit, file policy specified, display field mapping table, 2 missing tests added, hint heuristic relocated to dataclass property, `MAX_DECISIONS_PER_DOMAIN=200` cap.
