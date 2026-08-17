# Dream OTEL Metrics

> **Historical note (2026-07-13):** This document preserves the Claude-era design. A [provider-aware migration design](../../.specs/plans/dream-codex-agent-migration.design.md) now covers all six agent phases: Claude OTEL remains the rollback and historical path, while Codex uses JSONL. ROADMAP and EXTRACT remain outside this migration. This note does not indicate that live deployment is complete.

## Goal
Track tokens/cost/duration per dream phase via Claude Code's native OTEL telemetry.
Expose via brain-v42 sidecar `/metrics` endpoint for red-monitor dashboard.

## Architecture

```
claude -p (phase)
  stdout → logs/dream/{date}_{phase}.log  (report)
  stderr → logs/dream/{date}_{phase}_otel.log  (OTEL)
              |
              v
  dream_parser.py (CLI, sync psycopg2)
    regex parse api_request + tool_result events
    INSERT INTO dream_runs
              |
              v
  PostgreSQL dream_runs table
              |
              v
  sidecar /metrics → { "dream": { last_run, history } }
```

## Table: dream_runs

```sql
CREATE TABLE dream_runs (
    id SERIAL PRIMARY KEY,
    run_date DATE NOT NULL,
    phase VARCHAR(10) NOT NULL,
    model VARCHAR(30),
    status VARCHAR(10) NOT NULL,
    duration_s FLOAT,
    input_tokens INT DEFAULT 0,
    output_tokens INT DEFAULT 0,
    cache_read_tokens INT DEFAULT 0,
    cache_creation_tokens INT DEFAULT 0,
    cost_usd FLOAT DEFAULT 0,
    api_calls INT DEFAULT 0,
    tool_calls INT DEFAULT 0,
    created_at TIMESTAMPTZ DEFAULT NOW()
);
CREATE INDEX idx_dream_runs_date ON dream_runs(run_date DESC);
```

No retention — ~260 rows/year, keep all for cost trend analysis.

## dream_parser.py

CLI: `python -m brain_v42.metrics.dream_parser --phase scan --model sonnet --date 2000-01-01 --status done --duration 42 --project-key example-project otel.log`

> **Cette ligne ÉCRIT une row en base.** La date et la clé de projet sont
> délibérément impossibles. Le 2026-08-09, un agent de revue en lecture seule a
> recopié cet exemple — alors daté `2026-04-05`, donc plausible — et a inséré une
> vraie ligne dans `dream_runs` de production ; elle a faussé le `max(created_at)`
> de `dream_preflight` au point de manquer faire sauter les phases chères de la
> nuit suivante. Une date impossible rend la même erreur visible d'un coup d'œil.
> `--project-key` est requis et sans défaut depuis le lot du 2026-08-09.

- Regex parse `api_request` events → sum tokens, cost, count api_calls
- Regex parse `tool_result` events → count tool_calls
- INSERT into dream_runs via sync psycopg2 (one-shot CLI, not async)
- Empty OTEL file (dry run) → insert with tokens=0

## dream.sh changes

- Separate stdout/stderr: `> report.log 2> otel.log`
- Set OTEL env vars before claude -p: `CLAUDE_CODE_ENABLE_TELEMETRY=1 OTEL_LOGS_EXPORTER=console OTEL_METRICS_EXPORTER=console`
- After each phase: call parser with phase/model/date/status/duration
- Measure duration via bash SECONDS variable

## Sidecar /metrics extension

New `collect_dream_metrics()` in collector.py:
- Last run: `SELECT * FROM dream_runs WHERE run_date = (SELECT MAX(run_date) FROM dream_runs)`
- History: `SELECT run_date, SUM(cost_usd), SUM(tokens) ... GROUP BY run_date ORDER BY run_date DESC LIMIT 10`

JSON output adds `"dream"` section with `last_run` (per-phase detail) and `history` (10 recent runs summary).

## Files

| File | Action | ~LOC |
|------|--------|------|
| alembic/versions/016_dream_runs.py | New | 30 |
| src/brain_v42/metrics/dream_parser.py | New | 120 |
| src/brain_v42/metrics/collector.py | Edit | +40 |
| src/brain_v42/metrics/server.py | Edit | +10 |
| scripts/dream.sh | Edit | +20 |
| tests/unit/metrics/test_dream_parser.py | New | 150 |
| tests/unit/metrics/test_dream_metrics.py | New | 80 |
