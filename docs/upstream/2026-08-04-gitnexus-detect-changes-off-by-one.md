# Upstream issue draft — GitNexus 1.6.9

**Status: DRAFT. NOT PUBLISHED. Do not open this issue without an explicit operator decision.**

Target repository: https://github.com/abhigyanpatwari/GitNexus

Supersedes the earlier draft recorded in Brain ticket `7a08b8ff` (message 4), which framed the
problem as nondeterminism. That framing was wrong: in the supported configuration
`detect_changes` is strictly deterministic. The defect below is a *stable* false negative,
which is worse for a pre-commit gate because it never looks like a failure.

---

## Title

`detect_changes` silently drops any hunk confined to a symbol's last line (off-by-one between
0-based indexed ranges and 1-based git line numbers)

## Environment

- GitNexus 1.6.9 (`/home/hawixs/.npm-global/bin/gitnexus`)
- Node v22.22.0, Linux 6.8.0-124-generic x86_64
- Graph provider: ladybugdb, FTS: ladybugdb-fts
- Index built with `gitnexus analyze --index-only --force`
- Index commit == git HEAD == worktree commit (`de66363b`), 19 471 nodes / 37 833 edges
- Single registry entry for the repository; canonical root passed as an absolute path

## Summary

The hunk-to-symbol mapping compares 1-based git line numbers against line ranges that the index
stores 0-based, without converting between them. The effective matching window for every symbol
is therefore shifted up by exactly one line.

Two consequences, both silent:

1. A change confined to the **last line** of a symbol is attributed to **no symbol at all**.
   `detect_changes` returns `changed_count: 0`, `affected_count: 0`, `risk_level: "low"` and an
   empty `changed_symbols`, while still reporting `changed_files: 1`.
2. A change to the **blank line immediately above** a symbol's `def` is attributed **to that
   symbol**.

This matters because the last line of a function is almost always its `return`, and a
single-statement function (`def` + docstring + `return`) has its only executable line on the
blind line — such a function is entirely invisible to change detection.

## Reproduction

Fixed setup: index commit == HEAD == worktree commit; a Python file with

```python
30:                                     # blank
31: def short_id(uuid_val: str | UUID) -> str:
32:     """Return the full UUID string."""
33:     return str(uuid_val)
```

The index stores this symbol as `startLine: 30, endLine: 32` (0-based), confirmed via Cypher:

```cypher
MATCH (f:Function)
WHERE f.filePath = 'src/brain_v42/mcp/tools/formatters.py' AND f.name = 'short_id'
RETURN f.name, f.startLine, f.endLine
-- short_id | 30 | 32
```

Four probes, one edit each, same index, same worktree, `scope: "unstaged"`:

| # | Edited line (1-based) | Edit | Expected | Actual |
|---|---|---|---|---|
| 1 | 30 — blank line above `def` | replace blank with a comment | not attributed to `short_id` | **`short_id` reported**, risk `high`, 14 affected processes |
| 2 | 32 — docstring | insert a comment before it | `short_id` reported | `short_id` reported, risk `high`, 14 processes (correct) |
| 3 | 33 — `return str(uuid_val)` | → `return str(uuid_val).strip()` | `short_id` reported | **`changed_count: 0`, `affected_count: 0`, `risk_level: "low"`, `changed_symbols: []`** |
| 4 | 662 — `return body`, last line of a 70-line function | append a trailing comment | that function reported | **`changed_count: 0`, `risk_level: "low"`** |

Probe 4 shows the rule does not depend on function size.

In probes 1 and 3 the git hunk header did **not** name `short_id` (it showed a neighbouring
`import` line), yet probe 1 matched and probe 3 did not. This rules out the competing hypothesis
that the mapping keys off git's hunk-header function context — the discriminator is the line
range alone.

## Expected

A hunk overlapping `[startLine, endLine]` of a symbol is attributed to that symbol, with
consistent line-number bases on both sides of the comparison. A change on a symbol's last line
must be reported.

## Suggested fix / tests

1. Normalise line bases at the mapping boundary — convert git's 1-based hunk lines to the
   index's 0-based convention (or vice versa) in exactly one place, and assert the invariant.
2. Regression fixture: for a symbol spanning lines `[a, b]`, assert that an edit to `a`, to any
   interior line, and to `b` are each attributed to it, and that an edit to `a-1` and to `b+1`
   are not.
3. Include a single-statement function (`def` + docstring + `return`) in the fixture — it is the
   degenerate case where the bug hides the entire body.

## Secondary observations (same report, lower severity)

**Unstable result ordering.** On a stale index, repeated `detect_changes` calls on an unchanged
diff return the same *set* of `changed_symbols` in a varying *order* (a symbol moved from third
to first position between runs). The set and the risk level stay constant. This alone makes every
raw-output digest differ between runs while nothing semantically changes. A deterministic
`ORDER BY` before formatting would remove the noise.

**Stale index is reported as exact.** When the index commit is behind the working tree, results
are still labelled `epistemic: exact` rather than degraded or refused. Observed in a separate
repository (`red-writer`): on a stale index, six React components each returned `CRITICAL` with
109–179 direct callers and unrelated Python execution flows attached; after re-analysis at the
current commit the same calls returned `LOW` with 1–3 direct callers and no cross-language flows.
An explicit index-vs-HEAD provenance field, and an option to refuse rather than answer on a stale
index, would let callers fail closed.

**Silent exit code 1.** In the same `red-writer` observation, `npx gitnexus analyze` exited with
code 1 and empty output while having successfully written an index at the current commit
(`gitnexus status` reported up-to-date immediately afterwards).

## Local workaround in use

Documented as a runbook on our side: require index commit == HEAD; pass an absolute repo path;
pass `worktree` explicitly when changes live in a linked worktree; reject any result where
`changed_files > 0` but `changed_count == 0`; and when a symbol is known to be edited but not
reported, re-probe with a comment inserted mid-symbol to obtain the true blast radius.
