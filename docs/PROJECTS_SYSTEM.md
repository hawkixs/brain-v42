# The PROJECTS system, end to end

This document exists because it didn't. brain-v42's project system is spread across four
bricks — a format, three tables, a naming convention, and a pipeline — none of which
referred back to the others. You could read each one without ever learning that the key
is immutable, that the hierarchy is a typographic illusion, or that the predicate
implementing it lives in twelve copies.

**How to read this document.** It is organized by a grid: **each field of the model is
either a FACT the server observes or derives, or a JUDGMENT a human declares — never
both.** This grid is not decorative: it says who is allowed to write what, and it is
what makes design errors visible (§7).

**What this document is not.** It does not describe the target of a redesign. It
describes the verified state of the repo and the database. The numbers it cites are
**dated and perishable**; the command to replay them is given instead of the number
alone.

---

## 1. Brick 1 — The key format

Source of truth on the code side: `src/brain_v42/models/project_key.py`.

- **Canonical regex**: `^[a-z0-9]+([:-][a-z0-9]+)*$`. Kebab-case, with `:` accepted as a
  separator on the same footing as `-`.
- **Two auto-canonicalized aliases**, matched exactly, case-sensitive: `brain` and
  `brain_v42` become `brain-v42`.
- **Write/read asymmetry, deliberate.** `canonicalize_project_key(value, strict=True)` —
  the default, so the write path — raises `ValueError` with a suggestion on any
  non-conforming key. `strict=False` — the read path — passes it through as-is: a read
  with a bad key simply returns zero results, instead of failing an otherwise harmless
  query.
- `None` passes through unchanged: that's global knowledge, unscoped.
- `ProjectKeyCanonicalMixin` applies the rule to any Pydantic model that declares a
  `project_key`.

**The property that matters, and that must never be allowed to regress**: *a bad key
cannot be persisted*. It was earned after a drift incident where artifacts were written
under `brain_v42` (underscore) instead of `brain-v42`.

---

## 2. Brick 2 — Three surfaces in the database, often confused

| Table | Born in | Role | What people wrongly assume |
|---|---|---|---|
| `projects` | 033 | **Registry.** PK `project_key`, `registry_status` ∈ {claimed, unclaimed, archived}, `source` ∈ {context, reference, manual} | That this is the operational object. It isn't |
| `project_aliases` | 033 | **Alias table.** `alias_key` → `project_key`, FK CASCADE. Triggers enforce the rule on writes | That the application code consults it — canonicalization actually lives in the code |
| `project_contexts` | **001** | **The real operational object.** `current_focus`, `focus_revision`, `focus_updated_at`, `related_projects`, `project_group`, roadmap, counters | That it was born with the registry. It precedes it by thirty-two migrations |

**The registry follows the context, not the other way around.** Creating a
`project_context` registers a `claimed` entry; a plain reference from another project
creates `unclaimed`; deleting the context puts the row back to `unclaimed/reference`.
The registry row is therefore never a decision in itself — it's an observed consequence.

**Since 033, `project_contexts.project_key` is IMMUTABLE.** A trigger raises
`project_contexts.project_key is immutable` on any UPDATE of that column. Renaming a
project requires an explicit migration. This isn't an ergonomic oversight: it's what
makes key drift irreversible-by-accident.

**Knowledge tables carry the key WITHOUT a foreign key.** `decisions`, `learnings` and
`snippets` have it nullable; `runbooks`, `adrs` and `indexed_plans` require it NOT NULL.
Consistency therefore doesn't rest on the engine but on the Pydantic boundary plus the
033 triggers. That's a choice, and it has a price: nothing in the database prevents a
knowledge key from naming a project that doesn't exist.

**The `projects` CHECK and the code's regex are identical** — verified on 2026-08-19,
character for character: `^[a-z0-9]+([:-][a-z0-9]+)*$` on both sides
(`projects_key_format_valid` in the database, `_KEBAB` in the code). See §8 for what,
today, does *not* guarantee they stay that way.

---

## 3. Brick 3 — The hierarchy is FLAT

`red-shrik:agent` is not a child of `red-shrik`. **No parent/child link exists in the
database.** The colon is a naming convention, nothing more: the regex accepts it as an
ordinary separator, on the same footing as the hyphen.

Everywhere the code compares projects, it's by **strict equality**. The nightly pipeline
filters `project_key = :pk`; there is no prefix filter anywhere. Direct and often
surprising consequence: *a `red-lab` night never sees `red-lab:architect`*, even though
both run.

The exception to this strict equality is the **group scope** — and that's where the
sub-partition predicate lives.

---

## 4. The census of the colon predicate

**This census was wrong three times, each time from a different blind spot.** It is
reproduced here with its method, so the next person can redo it instead of trusting it.

- A first version claimed "a single exception in the entire codebase".
- A second corrected it to "three `src/` copies and two views".
- A third, searching for Python copies with the pattern `":" in `, missed the one
  written `":" not in` — the correction's own grep had its own blind spot.

**Count verified on 2026-08-19: five copies in `src/`, seven views in the database,
three distinct phrasings of the same predicate.**

### Three phrasings

| # | Form | Where |
|---|---|---|
| 1 | **SQL**, `base_key NOT LIKE '%:%' AND candidate LIKE base \|\| ':%'` | `db/project_group_scope.py:24` · `services/project_group_ticket_service.py:134` · `services/proposal_service.py:380` |
| 2 | **Python**, `":" not in base_key and project_key.startswith(f"{base_key}:")` | `services/project_group_ticket_service.py:163-166` |
| 3 | **SQL**, `split_part(project_key, ':', 1)` | `repositories/pg_project_context.py:202-204` |

Three observations that explain why the count resisted:

- **#2 lives in the SAME method as its SQL twin** (`_lock_participants_scope`). The
  predicate is written twice there, in two languages, thirty lines apart.
- **#3 is invisible to a grep on `not_like("%:%")`**: it uses neither `LIKE` nor `%:%`.
- **`proposal_service.py` copies the SQL even though it already imports
  `project_group_scope`** — the shared helper exists and isn't used there.

### Seven views in the database

Measured:

```sql
SELECT table_name FROM information_schema.views
WHERE table_schema = 'public' AND view_definition LIKE '%split_part%'
ORDER BY 1;
```

`codex_brain_entity_v1`, `codex_feature_artifact_v1`, `codex_feature_v1`,
`codex_roadmap_curation_proposal_v1`, `codex_ticket_extraction_proposal_v1`,
`codex_ticket_message_v1`, `codex_ticket_v1`.

All from migration **036**, and born from **two copied CTE bodies**: `_RED_KEYS_CTE`
(six views) and `_BRAIN_RED_KEYS_CTE` (one). Migration 024 is not a second living
object: 036 replaces its view with `CREATE OR REPLACE`.

**Total: twelve objects encode the same semantics**, five in Python/SQLAlchemy and seven
in SQL frozen in a migration. None references the others.

---

## 5. Brick 4 — The numbers, and how to replay them

**Do not copy any number from this section.** Replay it:

```bash
python3 docs/design/refonte-projets-sessions/baseline/snapshot.py
```

Measured on **2026-08-19** (Alembic head `045`), given as an order of magnitude and not
as a reference: 59 `project_contexts`; roughly 4,560 knowledge artifacts; **537
artifacts under a colon key**, spread over six keys. The heaviest, `red-shrik:agent`
(314), weighs **more than its parent** `red-shrik` (246) — which has no structural
consequence, since the parent/child link doesn't exist (§3), and the entire practical
consequence: these are two corpora that don't see each other.

A 2026-08-08 measurement cited "479 colon artifacts"; it was correct on its date and was
copied forward for ten days after it stopped being so. That is the failure mode this
section is meant to make impossible.

---

## 6. The three surfaces as seen by a caller

| Surface | What it exposes | Canonicalization |
|---|---|---|
| MCP tools `brain_*` | `project_key` as argument | **Strict** on write, **tolerant** on read |
| `codex_*` views | Read-only, group scope | Frozen in 036's SQL |
| Nightly pipeline | Per-project scope | Strict equality, no prefix |

---

## 7. The FACT / JUDGMENT grid applied

This is the grid announced up top. It says who writes what.

| Field | Nature | Who writes it |
|---|---|---|
| `projects.registry_status`, `source` | **FACT** | The server, as a consequence of a context created or referenced |
| `project_aliases.*` | **FACT** | 033 triggers |
| `project_contexts.focus_revision` | **FACT** | Trigger (032) — a counter, never an opinion |
| `project_contexts.focus_updated_at` | **FACT** | Application code (`db/focus_stamp`), under `IS DISTINCT FROM`: rewriting the same focus does not make it younger. `NULL` = never measured |
| `decisions_count`, `learnings_count`, … | **FACT** | Derived counters |
| `project_contexts.current_focus` | **JUDGMENT** | The human. **The system's only channel of free judgment** |
| `project_contexts.blockers` | **JUDGMENT** | The human |
| `description`, `code_style`, `git_workflow`, `test_strategy` | **JUDGMENT** | The human |
| Roadmap (`features`) | **JUDGMENT** | The human, assisted by proposals |

**What the grid makes visible.** `current_focus` is the only place in the system where
non-derivable judgment is written. Everything else is recomputable. The rule that
follows from this — and that is easy to violate without noticing — is that **the focus
must contain only what isn't already measurable elsewhere**: copying an artifact count
or a migration status into it turns the system's one judgment channel into a stale
cache.

**One row in the table is ambiguous**, and it's worth saying so: `current_phase` is
declared by the human but describes a state the system could often derive. It's a field
to watch — it ages poorly.

---

## 8. Known and unguarded drifts

- **The key regex exists in seventeen places across the tree, spread over sixteen
  files** (measured on 2026-08-19, excluding this document), and yet `project_key.py`
  declares itself the "single source of truth". **No test links `_KEBAB` to the SQL
  CHECK**: the 033 test pins the migration's source against a literal rewritten in the
  test, without ever importing `_KEBAB`. Widening the Python regex would therefore let
  through, on the Pydantic side, keys the database would refuse — and nothing would turn
  red before the INSERT. The breakdown is more instructive than the number: two
  migrations (012, 033), three `src/` modules, two tests, four documents, and **five
  recovery-attestation assets** (`ops/recovery/`, versions v2 to v4 and their
  `pgrestore` variants). Touching the regex therefore doesn't just break code/database
  consistency: it also breaks the restore proof.
- **The colon predicate lives in twelve copies** (§4), two of them in the same method. A
  change to sub-partition semantics must be applied twelve times.
- **Knowledge tables have no FK to the registry** (§2). A knowledge key can name a
  project that doesn't exist without the database objecting.
- **`project_contexts.current_focus` can be wiped by omission.** The `ON CONFLICT`
  branch of the upsert rewrites the focus to `NULL` when the argument is omitted. This
  channel exists in the code; it has **never been observed to bite** — the 10 contexts
  with `NULL` focus measured on 2026-08-19 are **all** at `focus_revision = 0` and never
  dated, so "never written", not "wiped".

---

*Verifications for this document, 2026-08-19, read-only: `models/project_key.py`;
`db/project_group_scope.py`; `services/project_group_ticket_service.py`;
`services/proposal_service.py`; `repositories/pg_project_context.py`;
`alembic/versions/001_initial.py`, `033_graph_relation_ledger.py`,
`036_codex_contract_views.py`; `tests/unit/db/test_schema_data_foundation_033.py`; and
the production database for views, constraints, and cardinalities. No writes, no commits
outside this file.*
