# Declaration — what a restore changes, and doesn't change, in a semantic search

**Measured on 2026-08-23 on the REAL embeddings of `brain-v42` production.** Ticket
`cfd26e9d`. This page is a DECLARATION, not a repair procedure: it serves to decide,
after a restore, whether an observed gap is noise or a failure.

---

## 0. Read before anything else: today, the question doesn't arise

**None of the 9 HNSW indexes are used by production.** Measured on 2026-08-23:

```
select s.relname, s.indexrelname, s.idx_scan
from pg_stat_user_indexes s
join pg_class c on c.oid = s.indexrelid
join pg_am    a on a.oid = c.relam
where a.amname = 'hnsw'
order by s.idx_scan desc;
```

→ `idx_scan = 0` on all nine, after 12d 21h of uptime. On the SAME table at the SAME
instant, `learnings_pkey` counts 894,194 scans and the FTS GIN index 5,071: the counter
works, the HNSW index is simply never reached.

The planner prefers a `Seq Scan` + exact sort: on `learnings`, 620 against 1,827 for a
forced HNSW scan. A single table, `indexed_plan_chunks`, picks HNSW on a bare query —
but its actual production query carries a mandatory join to `indexed_plans` and two
status filters, and falls back to `Hash Join` + two `Seq Scan`.

**Consequence**: `brain-v42`'s semantic search is today an EXACT brute-force KNN. HNSW
rebuild non-determinism **has no effect on results shown to a human.** The churn
described below is CONDITIONAL — it only exists if the index becomes used again.

### When this declaration will stop being true

The switchover is driven by the number of PAGES, hence by corpus size. Measured through
successive inserts on a disposable copy of production:

| `learnings` | relpages | plan chosen |
|---|---|---|
| 3,167 (today) | 512 | `Seq Scan` |
| 5,567 | 863 | `Seq Scan` |
| **5,967** | **924** | **`Index Scan using idx_learnings_embedding`** |

**Threshold: ~5,800 rows on `learnings`, against 3,167 today** — about 82% margin. At
13.4 rows/day (rate measured over 90 days) that's ~6.5 months; at August's rate
(606/month) ~4.5 months. **The trigger is the ROW COUNT, not the date.**

> **Check to redo before trusting this page**: replay the `idx_scan` query above. If a
  single one of the nine counters is non-zero, section 0 is stale and sections 1–3
  become the current regime.

---

## 1. What a restore NEVER changes

`pg_dump` carries **no graph at all**: the archive's TOC holds 9 `INDEX` entries and
zero bytes of HNSW graph. The nine indexes are **rebuilt** on restore.

Despite this, on the EXACT path — the one production uses:

| Comparison | top-10 overlap | same SET | same ORDER |
|---|---|---|---|
| restore A vs restore B | **10,000 / 10** | 1544/1544 | 1544/1544 |
| production vs restore | 9,997 / 10 | 1540/1544 | 1518/1544 |

The 26 queries that differ between production and restore differ **100% by
tie-breaking**: the sequence of the 10 distances is identical down to the digit (`max
|Δd| = 0.000e+00`). Two rows at a strictly equal distance come out in heap order, and a
freshly restored heap is compacted differently from an eight-month-old one.

> **No semantic loss. Zero.** An order that shifts between two perfectly tied rows is
  not a degradation.

---

## 2. The CONDITIONAL churn, if the index becomes used again

Measured by forcing an index scan (`set enable_seqscan = off`), on real embeddings, **n
= 1,544 queries**, top-k = 10, corpus = the 9 tables (7,555 vectors total).

### 2.1 Rebuild noise — seven independent measurements

| Reconstruction | overlap | same SET |
|---|---|---|
| `reindex` pass 1 | 9,847 | 96.6% |
| `reindex` pass 2 | 9,901 | 96.9% |
| `reindex` pass 3 | 9,814 | 96.2% |
| `reindex`, `max_parallel_maintenance_workers=0` | 9,842 | 96.4% |
| `reindex`, `max_parallel_maintenance_workers=2` | 9,869 | 96.8% |
| `drop` + `create` | 9,836 | 96.6% |
| **restore A vs restore B** | **9,866** | **96.8%** |

**NOISE BAND: 9.81 – 9.90 overlap, 96.2 – 96.9% of queries identical at top-10.**

Two independent restores of the SAME dump land at 9.866 — **inside** the band. A restore
therefore adds **nothing** to the noise of a plain rebuild.

Disabling parallel build fixes nothing (9.842 against 9.869): the sequential regime is
marginally the more unstable of the two.

### 2.2 It's not the graph that moves, it's the ties

Comparing DISTANCES rather than identifiers:

| Comparison | queries that differ | of which pure tie-breaking | of which real churn | `max abs delta d` |
|---|---|---|---|---|
| rebuild vs restore | 69 / 1544 (4.5%) | **84.1%** | 15.9% | 6.7e-03 |
| live production vs restore | 227 / 1544 (14.7%) | 63.0% | 37.0% | 3.0e-01 |

The tables richest in duplicates churn the most, and this churn is **entirely
fictitious**:

| table | distinct vectors | rebuild churn | of which ties |
|---|---|---|---|
| `gitlab_events` | 166 / 239 (69.5%) | 8.85 / 10 — the worst | **100%** |
| `indexed_plans` | 180 / 199 (90.5%) | stable set, order not | **100%** |
| `learnings` | 3,061 / 3,167 (96.7%) | 10.00 / 10 | — |
| `snippets`, `runbooks`, `adrs` | 100% | 10.00 / 10 | — |

`gitlab_events` has the worst-looking numbers and the most stable average distance in
the whole corpus (0.198894 across the three states, down to the digit). Its "churn" is
the shuffling of identical titles — "Merge branch…" — vectorized identically.

### 2.3 A case that DOES fall outside the band, and it isn't the restore

| Comparison | overlap | same SET |
|---|---|---|
| **live production vs restore** | **9.70 – 9.73** | **90.2%** |

Production's index is maintained **incrementally** over months; a restore **rebuilds it
in bulk**. These are two different graphs, and the gap is real — 37% of the divergences
shift a genuine distance, by up to 0.30. This is the figure an operator would observe,
and it's **bigger** than the gap between two restores.

---

## 3. Telling noise apart from a real degradation

### The rule

> **NEVER compare lists of identifiers.** Up to 15% of queries return different
  identifiers with no distance having moved at all. **Compare the NUMBER of rows
  returned, then the AVERAGE DISTANCE of the top-10.**

### The three signals, with their measured bands

| Signal | Healthy (measured) | Broken (measured) |
|---|---|---|
| rows returned per query | **10.000** | 1.095 (`ef_search` collapsed) |
| top-10 average distance | **0.31017 – 0.31027** | 0.31670 (`m=4`, `ef_construction=8`) |
| recall vs exact KNN | **0.974 – 0.979** | 0.820 |

The average distance moves by 1.0e-04 across the whole healthy band and by **6.5e-03 on
a real degradation: the signal is worth ~60 times the noise.** Row count alone catches
the `ef_search` collapse, which the average distance wouldn't show.

### The operator probe

To run against the restored database, then against a reference. No writes.

```sql
-- Replace <TABLE> and paste a real query vector (1536 floats).
-- Run TWICE: as-is (nominal path), then with `set enable_seqscan = off`
-- (forced HNSW path). Compare the two outputs.
set extra_float_digits = 3;
select count(*)              as lignes_rendues,   -- attendu : 10
       round(avg(d)::numeric, 6) as distance_moyenne
from (
  select embedding <=> '[…]'::vector as d
  from <TABLE>
  where embedding is not null
  order by embedding <=> '[…]'::vector
  limit 10
) s;
```

### Reading it

1. `lignes_rendues < 10` → **failure**. The index returns fewer candidates than
   requested.
2. `distance_moyenne` **more than 1% above** the reference → **failure** (a real `m=4`
   degradation gives +2.1%; rebuild noise gives +0.03%).
3. `distance_moyenne` within ±0.1% and 10 rows returned → **rebuild noise**, even if
   half the identifiers changed. Don't go looking for corruption.
4. Decisive comparison when no reference is available: replay the SAME query with `set
   enable_indexscan = off` (exact KNN, deterministic) and compare the average distance.
   **The exact↔HNSW gap is the recall**; it's the only absolute measure.

### Probe control, run in both directions

The probe was broken then repaired, to prove it reacts:

| State | average distance | rows | recall | identifier overlap |
|---|---|---|---|---|
| healthy restore | 0.310173 | 10.000 | 0.9742 | reference |
| **broken** `m=4, ef_construction=8` | **0.316698** | 10.000 | **0.8196** | **8.222 / 45.9%** |
| **repaired** `m=16, ef_construction=64` | **0.310172** | 10.000 | **0.9727** | **9.836 / 96.6%** |

Break it → the probe leaves the band. Repair it → it comes back.

---

## 4. Why the synthetic figure should never have been published

An earlier measurement on **uniform synthetic vectors** claimed rebuild noise of **8.60
/ 10, 4 queries out of 20 unchanged (20%)**.

On real embeddings, the noise is **9.87 / 10 and 96.8% of queries unchanged.**

And above all — an index that is **actually degraded** (`m=4`) measures **8.222 / 10 and
40.9% of identifiers unchanged.**

> **The synthetic figure was WORSE than a real failure.** Published as "normal after a
  restore", it would have made invisible exactly the degradation it was meant to catch.
  That's the second failure mode — a real degradation hidden by declared noise — and it
  was two-tenths of a point away from being carved into this runbook.

---

## 5. A neighboring finding, not addressed here

Production carries `extversion = '0.8.2'`, but its `vector.so` has md5
`5cfaddef0e7c4931811a3384466259c3`, **identical to the `0.8.4-pg16` image binary** and
different from `0.8.2-pg16`'s (`5c971952ab066b12175bad518d32de79`). The image carries
only ONE `vector.so`, only ONE extension script, and `pg_available_extension_versions`
reports only `0.8.4` there.

**A database restored from this image declares `extversion = '0.8.4'`.** A contract
check comparing the source extension label to the target's **will therefore fail on a
perfectly healthy restore**. Noted in passing; the DR contract is not modified here
(ticket `2ed0d4e0`).
