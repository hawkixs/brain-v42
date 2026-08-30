# B8 spike replay — Claude Code session join

**Date**: 2026-08-19
**Version measured**: **Claude Code 2.1.234**
**Original spike**: `docs/upstream/2026-08-06-claude-otlp-session-join.md`
(version measured 2.1.220)
**Verdict**: **UNCHANGED — JOIN STILL IMPOSSIBLE**

## Why this replay

The original spike concludes itself: "Re-measure on every Claude Code version bump
rather than taking this conclusion on faith." Its two relays
(`2bd14b24`, `7ffe0e8a`) repeat the instruction word for word. B8 was therefore rated
"High (constraint)" on a measurement **four minor versions** stale, and
no phase of the redesign plan replayed it.

This replay is content item #6 of Phase 0 of the PLAN. It was executed **first**,
before any other Phase 0 work, for a specific reason: the 2026-08-19 framing
decided **Q9** (subagents inherit) and set the **automatic-opening
key to `(project, connection)`** relying on the premise "`X-Brain-Session`
is dead." If the measurement had changed, both decisions would reopen.

The original spike's OTLP component is **not** replayed: it feeds into no
decision of this plan.

## Method

Protocol from the original spike, question 1 only. A disposable receiver on
`127.0.0.1:4318` logs the headers of every request; `claude -p` launches a real
Claude Code session as a subprocess with a dedicated `--mcp-config` and
`--strict-mcp-config`.

The spike's MCP configuration — `${PWD}` serves as a **control**: if it does
expand, the expansion mechanism works and only the variable is at fault.

```json
{ "mcpServers": { "spike": {
    "type": "http", "url": "http://127.0.0.1:4318/mcp",
    "headers": { "X-Brain-Session": "${CLAUDE_CODE_SESSION_ID}",
                 "X-Brain-Agent":   "${PWD}" } } } }
```

**Both cases are played, and this is essential.** The original spike notes that its
first run "looked conclusive; it wasn't" — the calling session's environment
leaked into the subprocess and injected the **wrong** identifier into it. A
replay that tested only the current environment would reproduce the false positive.

## Result

| Parent environment | `X-Brain-Session` received | Verdict |
|---|---|---|
| `CLAUDE_CODE_SESSION_ID` **present** | `630e63c7-eb36-5491-8858-b48c70b46532` — **the PARENT's identifier**, that of the calling session | False positive, reproduced identically |
| `CLAUDE_CODE_SESSION_ID` **removed** (`env -u`) | `${CLAUDE_CODE_SESSION_ID}` — **literal, unexpanded** | Nominal case |

Control, in both cases: `X-Brain-Agent` receives `/tmp/b8spike`, i.e. `${PWD}`
correctly expanded. **The expansion mechanism works; it's the variable that
does not exist at the moment the MCP configuration is read.** Claude Code does not set
`CLAUDE_CODE_SESSION_ID` for itself before this read.

Passed through the server's normalizers, checked the same day:

| Input | Function | Output |
|---|---|---|
| `${CLAUDE_CODE_SESSION_ID}` | `normalize_session` | `None` |
| `630e63c7-…` (parent's id) | `normalize_session` | `630e63c7-…` — **accepted, and that's the danger** |
| `/tmp/b8spike` | `normalize_agent` | `b8spike` |

## Consequences

1. **B8 stops being "rated on a stale measurement."** This is outcome (a) anticipated by
   the PLAN: nothing is invalidated, accretion continues as-is. The
   `unattributed` line remains the **nominal** case, not a theoretical degraded case.
2. **The 2026-08-19 framing's decisions HOLD**: Q9 (subagent inheritance) and
   the `(project, connection)` opening key rested on this premise. They are
   confirmed by the measurement, not merely by the argument.
3. **Useful confirmation for the framing**: `normalize_agent('/tmp/b8spike')` returns
   `b8spike`, the **basename of the directory**. `X-Brain-Agent` therefore does carry the
   PROJECT, not the agent — which is exactly why it cannot distinguish a subagent
   from its carrier.
4. **The false positive is an active trap, not a historical one.** A `X-Brain-Session`
   received and *valid* is not proof of a join: `normalize_session` accepts it even though it
   designates the parent. Any future join attempt must test the
   environment-removed case, or it will silently attach every child session
   to its parent.

## Scope of this measurement

Dated and **perishable**, like the one it replaces. It holds for Claude Code
**2.1.234** and nothing else. To be replayed on the next version bump — the
original spike's instruction is not lifted by this replay, it is honored by it.

*Read-only: no DB write, no commit, no repo file touched outside
this document. Spike artifacts under `/tmp/b8spike/`, receiver stopped and port 4318
freed after measurement.*
