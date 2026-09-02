# 2026-07-03 project audit — gaps, forgotten topics, idea backlog

> Exhaustive multi-agent pass (9-agent workflow: 4 brain-DB/code/docs/trackers sweeps,
> 3 ideation lenses, synthesis + completeness critique — 43 raw findings, 21 ideas).
> Associated brain entries: decision `1b5fb11f` (invalidated soak + token fix),
> supersession `99e9f5f7`→`29df5dc8` (replatform), learnings `f13144b3` (sidecar),
> `d2ebb101` (DB security), `e3287cdf` (starved PROMOTE), `6fac2352` (dated commitments),
> `ac78436a` (ghost Spec C).

## Executive summary

The core of brain_v42 is solid (89% coverage, HTTP cutover shipped, Spec A PROMOTE in prod),
but the autonomous layer (nightly Dream) logged a BLIND night as "6/6 OK"
(4th false-green recurrence) and PROMOTE has materialized nothing since 06-26 with no
alert existing for it. Second structural pathology: the brain logs dated
commitments but never recalls them (≥6 rotten promises). Short-term value lies in
observability (brain_doctor, canary, vitality gate, PROMOTE alerting); structural
value is in having these pathologies carried by the memory model itself
(review_by, CONTRADICTS, cite_ratio). Spec B remains the big bet, to launch only
once the pipeline has become trustworthy again.

## Incident handled in-session (2026-07-03 morning)

- **Blind 07-03 Dream**: token absent from the user unit → 401 → 0 brain tools → lying
  "6/6 OK". Fix: `token.conf` drop-in (EnvironmentFile 0600), validated E2E (200 probe +
  headless claude -p loads 35 tools). **REORG soak: counter reset to zero, WET flip ≥ 07-06.**
  Morning check strengthened: `tool_calls > 0` mandatory on clean/connect/synth/reorg.

## Forgotten topics

| # | Topic | Since | Suggested action |
|---|-------|--------|-----------------|
| 1 | Embedding replatform: 06-27 rollback, SPOF NOT resolved, lying decision (✅ superseded `99e9f5f7`), residual dev-pc state (masked docker-ce, 9.2 GB tarball, orphaned PS1) | 06-27 | Mini-spec "WSL2 LAN exposure", decide between the 3 options (nssm/socat forwarder recommended), date the retry |
| 2 | Spec C RESONANCE never scheduled (0 runs, killswitch open, code merged) + brain-v42 at 1 domain edge vs 108 for red-shrik | 06-12 | Wiring post-CONNECT + 5 DRY nights (runbook `0a4467ca`) + domain backfill |
| 3 | cite_ratio anti echo-drift: empirically validated 05-08 (recent insights at 82-91% = CRITICAL), never coded | 04-25 | Execute `c81d976b`'s design (effort S) |
| 4 | Spec A §9: +4-week scope re-evaluation overdue by ~6 weeks; tombstone drill 0/11 promotions | 05-22 | Explicit decision (extend or re-defer with a date) + 30-45 min drill |
| 5 | Task 5.3 cutover gate never closed — reaper on a 15-min cadence with no decommission criterion | 06-29 | After 1 clean dream night + 48h stable: log-only reaper + proof decision |
| 6 | Spec B meta-synthesis promised "next" in April — 0 doc, 0 commit | 04-17 | Decide: MVP (see ideas) or explicit abandonment |
| 7 | Graph hardening Angle 2 (CONTRADICTS/APPLIED_TO) "future spec" never written | 04-24 | Write-time MVP (see ideas) |
| 8 | Code Mode wired 03-12, never tested or decided; fastmcc.experimental can break silently | 03-12 | Decide in <1h: single test or removal (recommended) |
| 9 | Test pollution in the prod corpus: "E2E test decision — DELETE ME" + ~15 entities, ZZQX sentinels, 2 junk specs | 06-24 | Manual purge ~30 min (no subsystem — over-engineering) |
| 10 | MCP completeness: semantic_search pagination + 3 Future Considerations deferred with no follow-up | 03-15 | Close explicitly (annotated won't-do or dated feature) |

## Gaps & debts (prioritized)

**HIGH**
1. ~~Blind dream token~~ ✅ fixed in-session (decision `1b5fb11f`)
2. **Systemic false-green**: `detect_terminal_failure` signature-only, `post_run_alert` only if FAIL_TOTAL>0; the `preflight=RUN × tool_calls=0` crossing raises the historical objection (dream_parser.py:80-109) — a generic fix is now possible
3. ~~Invalidated REORG soak~~ ✅ acted on (counter 0, flip ≥07-06)
4. **Starved PROMOTE**: 20/28 nights dedup_unavailable, 0 promotion since 06-26, zero alerting (learning `e3287cdf`); investigate the 06-13→25 wave ("tools absent from namespace" despite the blocking flag)
5. **SECURITY (critical)**: PG 5433 + Neo4j 7687 on 0.0.0.0, weak `brain:brain` creds documented — DB backdoor bypasses all the MCP hardening (learning `d2ebb101`)

**MEDIUM**
6. CI: 39 DB-backed tests for the PROMOTE/REORG validators never run (BRAIN_V42_TEST_DB_URL absent from jobs) + `scripts/dream/` outside the coverage gate
7. Thresholds: 8/9 hand-picked (including search_min_score=0.20), calibration stopped after 1 threshold
8. Systemic documentation drift: CLAUDE.md ("M5 in progress", "30 tools", "stdio"), README (34 vs 36 tools, 19 vs 27 migrations), dream.sh killswitch comments inverted, stale brain roadmap, 138 unchecked boxes across 3 shipped plans

**LOW**
9. Localized coverage: indexed_plan_search_service 22%, metrics/__main__ 42%, pg_indexed_plan_repo 51%
10. Flaky PytestUnraisableExceptionWarning (AsyncMock, full-suite only) open since 06-12
11. `feat/mcp-http-server-foundation` branch merged but not deleted; GitLab 0 issues (the brain IS the tracker — by design)

## Idea backlog (retained by the synthesis)

| Idea | Effort | Value | Note |
|------|--------|--------|------|
| `brain_doctor` + fail-fast canary at the head of dream.sh | S | high | PG/Neo4j/embedding/reranker/**auth** probe → capability manifest; abort + alert before paying Opus |
| `detect_anomalous_night()` vitality gate | M | high | `done`+BLOCKED, or `preflight=RUN × tool_calls=0`, or promote dedup_unavailable → fail/partial + alert; TDD with 07-03's logs as fixtures |
| PROMOTE throughput alerting (streaks + promotion-free nights) | S | high | Spec A's missing SLO; would have caught the 3 waves |
| SYNTH cite_ratio guardrail | S | high | Design already written (`c81d976b`): parse trailer + gauge + REQUIRED section + soft skip >60% |
| Dated commitments: `review_by` + `brain_commitments` + "overdue deadlines" briefing | M | high | Attacks pathology #1 (learning `6fac2352`) |
| Write-time CONTRADICTS (inline warning + edge + contested) | M | high | Real case: `29df5dc8` vs `a09fbfbf`; resolves Spec C follow-up #2 + Angle 2 |
| Domain classification backfill + coverage metric | S | medium | Unblocks the brain-v42 cross-project briefing (glue code) |
| Codified soak ledger (`dream_soak_nights` + flip refused without proof) | S | medium | The "manual" soak just proved it is falsifiable; secures all future dry→wet rollouts |
| Spec B MVP: weekly community digests (LazyGraphRAG) | L | high | PREREQUISITE: ideas 1-4 shipped first |

## Quick wins (~2h30)

- [x] Fix dream token + E2E validation *(done in-session)*
- [x] 07-03 soak invalidated, flip pushed back *(decision `1b5fb11f`)*
- [x] Supersede `29df5dc8` *(done: `99e9f5f7`)*
- [ ] Purge the corpus's test pollution (E2E DELETE ME, ZZQX, specs `346865e7`/`a7d3d3e4`)
- [ ] Brain roadmap: mark shipped features done, archive dead ones
- [ ] `git branch -d feat/mcp-http-server-foundation`
- [ ] Refresh CLAUDE.md + README (36 tools, 27 migrations, HTTP :8765, PROMOTE WET, M5 removed)
- [ ] dream.sh:18-33 killswitch comments + DONE banner on the 3 shipped plans
- [ ] Decide on Code Mode (removal recommended) with brain_log_decision

## Completeness critique (angles outside the report)

- **Data security**: checked, real hole → learning `d2ebb101` (HIGH priority above)
- **Backups**: PG covered by red-backup (daily 05:00, 7/7 OK as of 07-03); **Neo4j absent
  from backup** (reconstructible via reconcile — choice to document); **no restore drill**;
  pre-migration 03-02 dumps lingering in ~/backups/
- **Alembic**: clean (27 migrations, single head, complete down_revisions)
- **Not covered**: aggregated/monthly Dream API costs, pgvector perf/bloat, dependency CVEs
- **Figures to re-check before a numeric decision**: "15" pollution entities (16 counted),
  "20" dedup_unavailable (18 ids cited), "~$1.04/night" unsourced
