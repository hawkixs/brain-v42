# Dream v3 "Closing the Loop" — Brainstorm WIP

**Date:** 2026-04-17
**Status:** Brainstorm in progress — sections 1-3 validated, 4-7 pending

## Scope global

3 specs indépendants priorisés :
1. **A (actionabilité)** — en cours de design
2. **B (méta-synthèse)** — next
3. **C (cross-domain linking)** — last

## Decisions taken for Spec A (actionability)

| # | Décision | Choix |
|---|----------|-------|
| 1 | Trigger | Hybride : agent propose, user valide |
| 2 | Targets | ADR + Runbook seulement (v1) |
| 3 | UI | red-monitor dashboard SolidJS (nouveau service block) |
| 4 | Briefing Claude | `brain_session_start` pointer only ("X pending → red-monitor") |
| 5 | Accept/reject | red-monitor writes straight into PG brain-v42 (brain DSN exists) |
| 6 | Matérialisation | J+1, prochain cycle dream 3am |
| 7 | Data model | New table `dream_proposals` (operational data, not knowledge) |
| 8 | Pipeline | Nouvelle phase PROMOTE entre SYNTH et REORG (Opus, max_turns 50) |
| 9 | Nouveaux tools | brain_list_mature_insights, brain_create_proposal, brain_list_accepted_proposals, brain_materialize_proposal |
| 10 | Guardrails | Max 3 proposals/run, max 5 materializations/run, never cross-project_key |
| 11 | Cross-repo | Touche brain-v42 + red-monitor |

## Table `dream_proposals`

```sql
CREATE TABLE dream_proposals (
    id UUID PRIMARY KEY,
    source_insight_id UUID REFERENCES learnings(id),
    target_type TEXT NOT NULL,              -- 'adr' | 'runbook'
    draft_title TEXT NOT NULL,
    draft_content JSONB NOT NULL,
    status TEXT NOT NULL DEFAULT 'pending',  -- 'pending' | 'accepted' | 'rejected'
    status_changed_at TIMESTAMPTZ,
    materialized_entity_id UUID,
    created_at TIMESTAMPTZ DEFAULT now()
);
```

## Phase PROMOTE

- Position : SCAN → CLEAN → CONNECT → SYNTH → **PROMOTE** → REORG
- Model : Opus, max_turns 50, timeout 10m
- Étape (a) : matérialise proposals acceptées (status='accepted', materialized_entity_id IS NULL) via brain_propose_adr / brain_create_runbook existants
- Step (b): promotes mature insights (>7d, not already promoted) into new proposals

## Sections de design restantes

- **Section 4** : red-monitor dashboard integration (SolidJS service block, layout, PG queries, accept/reject UI)
- **Section 5** : brain_session_start briefing modification
- **Section 6** : PROMOTE prompt design
- **Section 7** : Testing strategy et guardrails complets
- Puis : write full spec → self-review → user review → invoke writing-plans
